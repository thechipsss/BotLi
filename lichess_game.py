from typing import Tuple

import chess
import chess.engine
import chess.polyglot
from chess.variant import find_variant

from aliases import CP_Score, Depth, Offer_Draw, Outcome, Resign, UCI_Move
from api import API
from enums import Game_Status, Variant


class Lichess_Game:
    def __init__(self, api: API, gameFull_event: dict, config: dict) -> None:
        self.config = config
        self.api = api
        self.board = self._setup_board(gameFull_event)
        self.username: str = self.api.user['username']
        self.white_name: str = gameFull_event['white'].get('name', 'AI')
        self.black_name: str = gameFull_event['black'].get('name', 'AI')
        self.is_white: bool = gameFull_event['white'].get('name') == self.username
        self.initial_time: int = gameFull_event['clock']['initial']
        self.increment: int = gameFull_event['clock']['increment']
        self.white_time: int = gameFull_event['state']['wtime']
        self.black_time: int = gameFull_event['state']['btime']
        self.variant = Variant(gameFull_event['variant']['key'])
        self.status = Game_Status(gameFull_event['state']['status'])
        self.draw_enabled: bool = config['engine']['offer_draw']['enabled']
        self.resign_enabled: bool = config['engine']['resign']['enabled']
        self.ponder_enabled: bool = self.config['engine']['ponder']
        self.move_overhead = self._get_move_overhead()
        self.out_of_book_counter = 0
        self.out_of_cloud_counter = 0
        self.out_of_chessdb_counter = 0
        self.engine = self._get_engine()
        self.scores: list[chess.engine.PovScore] = []
        self.last_message = 'No eval available yet.'

    def make_move(self) -> Tuple[UCI_Move, Offer_Draw, Resign]:
        if move := self._make_book_move():
            message = f'Book:    {self._format_move(move):14}'
            offer_draw = False
            resign = False
            engine_move = False
        elif response := self._make_cloud_move():
            uci_move, cp_score, depth = response
            move = chess.Move.from_uci(uci_move)
            pov_score = chess.engine.PovScore(chess.engine.Cp(cp_score), chess.WHITE)
            message = f'Cloud:   {self._format_move(move):14} {self._format_score(pov_score)}     {depth}'
            offer_draw = False
            resign = False
            engine_move = False
        elif uci_move := self._make_chessdb_move():
            move = chess.Move.from_uci(uci_move)
            message = f'ChessDB: {self._format_move(move):14}'
            offer_draw = False
            resign = False
            engine_move = False
        elif response := self._make_egtb_move():
            uci_move, outcome, offer_draw, resign = response
            offer_draw = offer_draw and self.draw_enabled
            resign = resign and self.resign_enabled
            move = chess.Move.from_uci(uci_move)
            message = f'EGTB:    {self._format_move(move):14} {outcome}'
            engine_move = False
        else:
            move, info = self._make_engine_move()
            message = f'Engine:  {self._format_move(move):14} {self._format_info(info)}'
            offer_draw = self._is_drawish()
            resign = self._is_resignable()
            engine_move = True

        print(message)
        self.last_message = message
        self.board.push(move)
        if not engine_move and self.ponder_enabled:
            self.engine.analysis(self.board)
        return move.uci(), offer_draw, resign

    def update(self, gameState_event: dict) -> bool:
        self.status = Game_Status(gameState_event['status'])

        moves = gameState_event['moves'].split()
        if len(moves) <= len(self.board.move_stack):
            return False

        self.board.push(chess.Move.from_uci(moves[-1]))
        self.white_time = gameState_event['wtime']
        self.black_time = gameState_event['btime']

        return True

    def get_result_message(self, winner: str | None) -> str:
        winning_name = self.white_name if winner == 'white' else self.black_name
        losing_name = self.white_name if winner == 'black' else self.black_name

        if winner:
            message = f'{winning_name} won'

            if self.status == Game_Status.MATE:
                message += ' by checkmate!'
            elif self.status == Game_Status.OUT_OF_TIME:
                message += f'! {losing_name} run out of time.'
            elif self.status == Game_Status.RESIGN:
                message += f'! {losing_name} resigned.'
        elif self.status == Game_Status.DRAW:
            if self.board.is_fifty_moves():
                message = 'Game drawn by 50-move rule.'
            elif self.board.is_repetition():
                message = 'Game drawn by threefold repetition.'
            elif self.board.is_insufficient_material():
                message = 'Game drawn due to insufficient material.'
            else:
                message = 'Game drawn by agreement.'
        elif self.status == Game_Status.STALEMATE:
            message = 'Game drawn by stalemate.'
        else:
            message = 'Game aborted.'

        return message

    def is_our_turn(self) -> bool:
        return self.is_white == self.board.turn

    def is_game_over(self) -> bool:
        return self.board.is_checkmate() or \
            self.board.is_stalemate() or \
            self.board.is_insufficient_material() or \
            self.board.is_fifty_moves() or \
            self.board.is_repetition()

    def is_abortable(self) -> bool:
        return len(self.board.move_stack) < 2

    def quit_engine(self) -> None:
        self.engine.quit()

    def _is_drawish(self) -> bool:
        if not self.draw_enabled:
            return False

        min_game_length = self.config['engine']['offer_draw']['min_game_length']
        consecutive_moves = self.config['engine']['offer_draw']['consecutive_moves']

        if self.board.fullmove_number < min_game_length or len(self.scores) < consecutive_moves:
            return False

        max_score = self.config['engine']['offer_draw']['score']

        for score in self.scores[-consecutive_moves:]:
            if abs(score.relative.score(mate_score=40000)) > max_score:
                return False

        return True

    def _is_resignable(self) -> bool:
        if not self.resign_enabled:
            return False

        consecutive_moves = self.config['engine']['resign']['consecutive_moves']

        if len(self.scores) < consecutive_moves:
            return False

        max_score = self.config['engine']['resign']['score']

        for score in self.scores[-consecutive_moves:]:
            if score.relative.score(mate_score=40000) > max_score:
                return False

        return True

    def _make_book_move(self) -> chess.Move | None:
        enabled = self.config['engine']['opening_books']['enabled']

        if not enabled:
            return

        out_of_book = self.out_of_book_counter >= 10
        max_depth = self.config['engine']['opening_books'].get('max_depth', float('inf'))
        too_deep = self.board.ply() >= max_depth

        if out_of_book or too_deep:
            return

        selection = self.config['engine']['opening_books']['selection']
        for book in self._get_books():
            with chess.polyglot.open_reader(book) as book_reader:
                try:
                    if selection == 'weighted_random':
                        move = book_reader.weighted_choice(self.board).move
                    elif selection == 'uniform_random':
                        move = book_reader.choice(self.board).move
                    else:
                        move = book_reader.find(self.board).move

                    self.out_of_book_counter = 0
                    new_board = self.board.copy()
                    new_board.push(move)
                    if not new_board.is_repetition(count=2):
                        return move
                except IndexError:
                    pass

        self.out_of_book_counter += 1

    def _get_books(self) -> list[str]:
        books = self.config['engine']['opening_books']['books']

        if self.board.chess960 and 'chess960' in books:
            return books['chess960']
        else:
            if self.is_white and 'white' in books:
                return books['white']
            elif not self.is_white and 'black' in books:
                return books['black']

        return books['standard'] if 'standard' in books else []

    def _make_cloud_move(self) -> Tuple[UCI_Move, CP_Score, Depth] | None:
        enabled = self.config['engine']['online_moves']['lichess_cloud']['enabled']

        if not enabled:
            return

        out_of_book = self.out_of_cloud_counter >= 10
        has_time = self._has_time(self.config['engine']['online_moves']['lichess_cloud']['min_time'])
        max_depth = self.config['engine']['online_moves']['lichess_cloud'].get('max_depth', float('inf'))
        too_deep = self.board.ply() >= max_depth

        if out_of_book or too_deep or not has_time:
            return

        timeout = self.config['engine']['online_moves']['lichess_cloud']['timeout']
        min_eval_depth = self.config['engine']['online_moves']['lichess_cloud']['min_eval_depth']

        if response := self.api.get_cloud_eval(self.board.fen(), self.variant, timeout):
            if 'error' not in response:
                if response['depth'] >= min_eval_depth:
                    self.out_of_cloud_counter = 0
                    return response['pvs'][0]['moves'].split()[0], response['pvs'][0]['cp'], response['depth']

            self.out_of_cloud_counter += 1
        else:
            self._reduce_own_time(timeout * 1000)

    def _make_chessdb_move(self) -> UCI_Move | None:
        enabled = self.config['engine']['online_moves']['chessdb']['enabled']

        if not enabled:
            return

        out_of_book = self.out_of_chessdb_counter >= 10
        has_time = self._has_time(self.config['engine']['online_moves']['chessdb']['min_time'])
        max_depth = self.config['engine']['online_moves']['chessdb'].get('max_depth', float('inf'))
        too_deep = self.board.ply() >= max_depth

        if out_of_book or too_deep or not has_time:
            return

        timeout = self.config['engine']['online_moves']['chessdb']['timeout']
        min_eval_depth = self.config['engine']['online_moves']['chessdb']['min_eval_depth']
        selection = self.config['engine']['online_moves']['chessdb']['selection']

        if selection == 'good':
            action = 'querybest'
        elif selection == 'all':
            action = 'query'
        else:
            action = 'querypv'

        if response := self.api.get_chessdb_eval(self.board.fen(), action, timeout):
            if response['status'] == 'ok':
                if response.get('depth', 50) >= min_eval_depth:
                    self.out_of_chessdb_counter = 0
                    return response['move'] if 'move' in response else response['pv'][0]

            self.out_of_chessdb_counter += 1
        else:
            self._reduce_own_time(timeout * 1000)

    def _make_egtb_move(self) -> Tuple[UCI_Move, Outcome, Offer_Draw, Resign] | None:
        enabled = self.config['engine']['online_moves']['online_egtb']['enabled']

        if not enabled:
            return

        is_endgame = chess.popcount(self.board.occupied) <= 7
        has_time = self._has_time(self.config['engine']['online_moves']['online_egtb']['min_time'])

        if not is_endgame or not has_time:
            return

        timeout = self.config['engine']['online_moves']['online_egtb']['timeout']

        if response := self.api.get_egtb(self.board.fen(), timeout):
            uci_move = response['moves'][0]['uci']
            outcome = response['category']
            return uci_move, outcome, outcome == 'draw', outcome == 'loss'
        else:
            self._reduce_own_time(timeout * 1000)

    def _make_engine_move(self) -> Tuple[chess.Move, chess.engine.InfoDict]:
        if len(self.board.move_stack) < 2:
            limit = chess.engine.Limit(time=10)
            ponder = False
        else:
            if self.is_white:
                white_time = self.white_time - self.move_overhead if self.white_time > self.move_overhead else self.white_time / 2
                white_time /= 1000
                black_time = self.black_time / 1000
            else:
                black_time = self.black_time - self.move_overhead if self.black_time > self.move_overhead else self.black_time / 2
                black_time /= 1000
                white_time = self.white_time / 1000
            increment = self.increment / 1000

            limit = chess.engine.Limit(white_clock=white_time, white_inc=increment,
                                       black_clock=black_time, black_inc=increment)
            ponder = self.ponder_enabled

        result = self.engine.play(self.board, limit, info=chess.engine.INFO_ALL, ponder=ponder)
        if result.move:
            score = result.info.get('score', chess.engine.PovScore(chess.engine.Mate(1), self.board.turn))
            self.scores.append(score)
            return result.move, result.info
        raise RuntimeError('Engine could not make a move!')

    def _format_move(self, move: chess.Move) -> str:
        if self.board.turn:
            move_number = str(self.board.fullmove_number) + '.'
            return f'{move_number:4} {self.board.san(move)}'
        else:
            move_number = str(self.board.fullmove_number) + '...'
            return f'{move_number:6} {self.board.san(move)}'

    def _format_info(self, info: chess.engine.InfoDict) -> str:
        info_score = info.get('score')
        score = f'{self._format_score(info_score):7}' if info_score else 7 * ' '

        info_depth = info.get('depth')
        info_seldepth = info.get('seldepth')
        depth_str = f'{info_depth}/{info_seldepth}'
        depth = f'{depth_str:6}' if info_depth and info_seldepth else 6 * ' '

        info_nps = info.get('nps')
        nps = f'nps: {info_nps/1000000:5.1f} M' if info_nps else 8 * ' '

        info_time = info.get('time')
        time = f'mt: {info_time:5.1f} s' if info_time else 11 * ' '

        info_hashfull = info.get('hashfull')
        hashfull = f'hash: {info_hashfull/10:4.1f} %' if info_hashfull else 12 * ' '

        info_tbhits = info.get('tbhits')
        tbhits = f'tb: {info_tbhits}' if info_tbhits else ''

        return '     '.join((score, depth, nps, time, hashfull, tbhits))

    def _format_score(self, score: chess.engine.PovScore) -> str:
        if not score.is_mate():
            if cp_score := score.pov(self.board.turn).score():
                cp_score /= 100
                return format(cp_score, '+7.2f')
            else:
                return '   0.00'
        else:
            return str(score.pov(self.board.turn))

    def _get_engine(self) -> chess.engine.SimpleEngine:
        engine = chess.engine.SimpleEngine.popen_uci(self.config['engine']['path'])
        options = self.config['engine']['uci_options']

        def not_managed(key: str): return not chess.engine.Option(key, '', None, None, None, None).is_managed()
        options = {key: value for key, value in options.items() if not_managed(key)}

        engine.configure(options)

        return engine

    def _setup_board(self, gameFull_event: dict) -> chess.Board:
        if gameFull_event['variant']['key'] == 'chess960':
            board = chess.Board(gameFull_event['initialFen'], chess960=True)
        elif gameFull_event['variant']['name'] == 'From Position':
            board = chess.Board(gameFull_event['initialFen'])
        else:
            VariantBoard = find_variant(gameFull_event['variant']['name'])
            board = VariantBoard()

        for move in gameFull_event['state']['moves'].split():
            board.push_uci(move)

        return board

    def _get_move_overhead(self) -> int:
        multiplier = self.config.get('move_overhead_multiplier', 1.0)
        return max(int(self.initial_time / 60 * multiplier), 1000)

    def _has_time(self, min_time: int) -> bool:
        min_time *= 1000
        return self.white_time >= min_time if self.is_white else self.black_time >= min_time

    def _reduce_own_time(self, milliseconds: int) -> None:
        if self.is_white:
            self.white_time -= milliseconds
        else:
            self.black_time -= milliseconds
