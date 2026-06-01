"""
durak_game.py
────────────────
Серверная логика игры "Дурак" (авторитетный сервер).

Podkidnoy focus (perevodnoy — заглушка на будущее).
Максимально строгая валидация ходов + удобные get_legal_* / get_allowed_actions
для клиентской интеграции.
"""

from enum import Enum
from dataclasses import dataclass
from typing import List, Optional
import random


class Suit(Enum):
    HEARTS = "♥"
    DIAMONDS = "♦"
    CLUBS = "♣"
    SPADES = "♠"


class Rank(Enum):
    SIX = 6
    SEVEN = 7
    EIGHT = 8
    NINE = 9
    TEN = 10
    JACK = 11
    QUEEN = 12
    KING = 13
    ACE = 14


@dataclass(frozen=True)
class Card:
    rank: Rank
    suit: Suit

    def __str__(self):
        return f"{self.rank.value}{self.suit.value}"

    def __repr__(self):
        return self.__str__()

    @staticmethod
    def from_str(s: str) -> "Card":
        """Парсит строку вида '10♥', 'J♠', 'A♦' обратно в Card."""
        if not s or len(s) < 2:
            raise ValueError(f"Bad card string: {s}")

        suit_char = s[-1]
        rank_str = s[:-1]

        suit_map = {"♥": Suit.HEARTS, "♦": Suit.DIAMONDS, "♣": Suit.CLUBS, "♠": Suit.SPADES}
        suit = suit_map.get(suit_char)
        if not suit:
            raise ValueError(f"Unknown suit in {s}")

        rank_map = {str(r.value): r for r in Rank}
        # Поддержка J/Q/K/A как букв
        rank_map.update({"J": Rank.JACK, "Q": Rank.QUEEN, "K": Rank.KING, "A": Rank.ACE, "11": Rank.JACK, "12": Rank.QUEEN, "13": Rank.KING, "14": Rank.ACE})

        rank = rank_map.get(rank_str)
        if not rank:
            # fallback для 6-10
            try:
                rank = Rank(int(rank_str))
            except Exception:
                raise ValueError(f"Unknown rank in {s}")

        return Card(rank=rank, suit=suit)


class Deck:
    """Колода карт для Дурака. Поддерживает 24, 36 и 52 карты."""

    def __init__(self, size: int = 36):
        if size not in (24, 36, 52):
            raise ValueError("Deck size must be 24, 36 or 52")

        self.size = size
        self.cards: List[Card] = self._create_deck()
        self.trump_suit: Optional[Suit] = None

    def _create_deck(self) -> List[Card]:
        if self.size == 52:
            ranks = list(Rank)
        elif self.size == 36:
            ranks = [r for r in Rank if r.value >= 6]
        else:  # 24
            ranks = [r for r in Rank if r.value >= 10]

        deck = [Card(rank=r, suit=s) for s in Suit for r in ranks]
        return deck

    def shuffle(self):
        random.shuffle(self.cards)

    def deal(self, num_players: int) -> List[List[Card]]:
        """
        Раздаёт карты по правилам Дурака.
        Возвращает список рук игроков (по 6 карт).
        Козырь определяется последней картой в колоде.
        """
        if num_players < 2 or num_players > 6:
            raise ValueError("Number of players must be between 2 and 6")

        self.shuffle()

        # Козырь — последняя карта в колоде
        self.trump_suit = self.cards[-1].suit

        hands: List[List[Card]] = [[] for _ in range(num_players)]

        # В настоящем Дураке карты раздаются по одной, начиная с игрока слева от сдающего.
        # Для простоты и детерминированности пока используем циклическую раздачу.
        cards_to_deal = 6 * num_players

        for i in range(cards_to_deal):
            player_index = i % num_players
            hands[player_index].append(self.cards.pop(0))

        return hands

    def draw_card(self) -> Optional[Card]:
        """Берёт одну карту из колоды (для добора)."""
        if self.cards:
            return self.cards.pop(0)
        return None

    def remaining_cards(self) -> int:
        return len(self.cards)

    def determine_first_attacker(self, hands: List[List[Card]]) -> int:
        """
        Определяет игрока, который ходит первым.
        По правилам Дурака — тот, у кого самая младшая козырная карта.
        Если козырей нет ни у кого — ходит игрок с самой младшей картой.
        Возвращает индекс игрока.
        """
        if not hands:
            raise ValueError("No players")

        lowest_trump = None
        lowest_trump_player = None

        lowest_card = None
        lowest_card_player = None

        for player_idx, hand in enumerate(hands):
            for card in hand:
                if card.suit == self.trump_suit:
                    if lowest_trump is None or card.rank.value < lowest_trump.rank.value:
                        lowest_trump = card
                        lowest_trump_player = player_idx
                else:
                    if lowest_card is None or card.rank.value < lowest_card.rank.value:
                        lowest_card = card
                        lowest_card_player = player_idx

        if lowest_trump_player is not None:
            return lowest_trump_player
        elif lowest_card_player is not None:
            return lowest_card_player
        else:
            return 0  # fallback

    def draw_cards(self, hand: List[Card], max_cards: int = 6) -> List[Card]:
        """
        Игрок добирает карты из колоды до max_cards (обычно до 6).
        Возвращает список добранных карт.
        """
        drawn = []
        while len(hand) < max_cards and self.cards:
            card = self.cards.pop(0)
            hand.append(card)
            drawn.append(card)
        return drawn


class DurakGame:
    """
    Основной класс игры "Дурак" (авторитетный сервер).
    Сильная валидация: wave accounting (players_who_threw_this_wave),
    is_legal_*, get_legal_*, can_*, get_allowed_actions, get_role.
    Поддерживает 2-6 игроков, 24/36/52 карты.
    """

    def __init__(self, player_ids: List[int], deck_size: int = 36, game_type: str = "podkidnoy"):
        if len(player_ids) < 2 or len(player_ids) > 6:
            raise ValueError("Дурак поддерживает от 2 до 6 игроков")
        if game_type not in ("podkidnoy", "perevodnoy"):
            game_type = "podkidnoy"

        self.player_ids = player_ids
        self.num_players = len(player_ids)
        self.game_type = game_type

        self.deck = Deck(deck_size)
        self.hands: dict[int, List[Card]] = {}
        self.trump_suit: Optional[Suit] = None

        self.current_attacker: Optional[int] = None
        self.current_defender: Optional[int] = None

        self.table: List[tuple[Card, Optional[Card]]] = []  # [(атака, отбой), ...]
        self.discard_pile: List[Card] = []

        self.game_over = False
        self.winner: Optional[int] = None

        # === Состояние текущей "волны атаки" ===
        self.attack_in_progress: bool = False
        self.attack_finished: bool = False

        # Игроки, которые уже подкинули карту в текущей волне атаки.
        # В настоящем Дураке игрок не может подкидывать второй раз в той же волне,
        # пока круг не завершится (защитник отбил всё или взял карты).
        self.players_who_threw_this_wave: set[int] = set()

    def start_game(self):
        """Начинает игру: раздаёт карты и определяет первого атакующего."""
        hands_list = self.deck.deal(self.num_players)
        self.trump_suit = self.deck.trump_suit

        for i, pid in enumerate(self.player_ids):
            self.hands[pid] = hands_list[i]

        # Определяем первого атакующего по правилам
        attacker_index = self.deck.determine_first_attacker(hands_list)
        self.current_attacker = self.player_ids[attacker_index]

        # Защитник — следующий игрок по кругу
        defender_index = (attacker_index + 1) % self.num_players
        self.current_defender = self.player_ids[defender_index]

        # Явный сброс состояния волны для новой раздачи
        self.attack_in_progress = False
        self.attack_finished = False
        self.table = []
        self.players_who_threw_this_wave.clear()

    def get_hand(self, player_id: int) -> List[Card]:
        return self.hands.get(player_id, [])

    def draw_for_players(self):
        """Все игроки добирают карты до 6 (если возможно)."""
        for pid in self.player_ids:
            if pid in self.hands:
                self.deck.draw_cards(self.hands[pid])

    # ====================== ЛОГИКА ХОДОВ ======================

    def attack(self, player_id: int, card: Card) -> bool:
        """
        Игрок подкидывает карту на стол во время атаки.
        В реальном Дураке это может делать не только текущий атакующий,
        но и любой другой игрок, у кого есть карта подходящего номинала,
        пока атака ещё открыта.
        """
        if self.game_over or self.attack_finished:
            return False
        if not self.attack_in_progress:
            # Только текущий атакующий может начать атаку первой картой
            if player_id != self.current_attacker:
                return False
            if self.table:  # уже есть карты — странная ситуация
                return False

        hand = self.hands.get(player_id, [])
        if card not in hand:
            return False

        # Проверка лимита
        if self.get_max_attack_cards_remaining() <= 0:
            return False

        # Проверка номинала
        if self.table:
            table_ranks = {c.rank for pair in self.table for c in pair if c is not None}
            if card.rank not in table_ranks:
                return False

        # Всё ок
        hand.remove(card)
        self.table.append((card, None))

        # Отмечаем, что игрок поучаствовал в этой волне
        self.players_who_threw_this_wave.add(player_id)

        # Если это первый ход в атаке — отмечаем, что атака началась
        if not self.attack_in_progress:
            self.attack_in_progress = True

        return True

    def beat(self, player_id: int, attack_card: Card, beat_card: Card) -> bool:
        """
        Защищающийся отбивает конкретную атакующую карту.
        Усиленная валидация.
        """
        if self.game_over or self.attack_finished:
            return False
        if player_id != self.current_defender:
            return False

        defender_hand = self.hands.get(player_id, [])
        if beat_card not in defender_hand:
            return False

        all_table_cards = [c for pair in self.table for c in pair if c is not None]
        if beat_card in all_table_cards:
            return False

        unbeaten = self._get_unbeaten_count()
        if len(defender_hand) < unbeaten:
            return False

        for i, (atk, bt) in enumerate(self.table):
            if atk == attack_card and bt is None:
                if not self._can_beat(atk, beat_card):
                    return False

                defender_hand.remove(beat_card)
                self.table[i] = (atk, beat_card)
                return True

        return False

    def _can_beat(self, attack_card: Card, beat_card: Card) -> bool:
        """Можно ли отбить карту."""
        if beat_card.suit == self.trump_suit:
            if attack_card.suit == self.trump_suit:
                return beat_card.rank.value > attack_card.rank.value
            return True
        if attack_card.suit == beat_card.suit:
            return beat_card.rank.value > attack_card.rank.value
        return False

    def take_table(self, player_id: int) -> bool:
        """Защищающийся забирает все карты со стола."""
        if player_id != self.current_defender:
            return False
        if not self.can_take_table():
            return False

        # Забираем все карты (и атаки, и отбои) в руку защитника
        cards_taken: List[Card] = []
        for atk, bt in self.table:
            if atk:
                cards_taken.append(atk)
            if bt:
                cards_taken.append(bt)
        for c in cards_taken:
            self.hands[player_id].append(c)

        # Ротация: после взятия следующий атакующий — игрок ПОСЛЕ защитника
        defender_index = self.player_ids.index(self.current_defender)
        new_attacker_index = (defender_index + 1) % self.num_players
        new_defender_index = (new_attacker_index + 1) % self.num_players

        self.current_attacker = self.player_ids[new_attacker_index]
        self.current_defender = self.player_ids[new_defender_index]

        # Общий сброс раунда (карты НЕ идут в сброс, а в руку)
        self._cleanup_round(to_discard=None)
        # attack_finished оставляем как маркер, что раунд завершён (для клиента)
        self.attack_finished = True

        return True

    def finish_attack(self) -> bool:
        """
        Завершает текущий кон (все карты успешно отбиты).
        По правилам Дурака после успешной защиты следующий атакующий — бывший защитник.
        """
        if not self.can_finish_attack():
            return False

        # Все карты на столе (атаки + отбои) идут в сброс
        to_discard: List[Card] = []
        for atk, bt in self.table:
            if atk:
                to_discard.append(atk)
            if bt:
                to_discard.append(bt)

        # Ротация: после 'бито' бывший защитник становится атакующим
        defender_index = self.player_ids.index(self.current_defender)
        new_attacker_index = defender_index
        new_defender_index = (defender_index + 1) % self.num_players

        self.current_attacker = self.player_ids[new_attacker_index]
        self.current_defender = self.player_ids[new_defender_index]

        # Общий сброс раунда + перемещение в сброс
        self._cleanup_round(to_discard=to_discard)
        self.attack_finished = True

        return True

    def get_table(self) -> List[tuple[Card, Optional[Card]]]:
        return self.table[:]

    def can_attack_more(self) -> bool:
        """Может ли текущий атакующий подкинуть ещё карту?"""
        if self.game_over or self.attack_finished:
            return False
        return self.get_max_attack_cards_remaining() > 0

    def is_attack_still_possible(self) -> bool:
        """
        Можно ли в принципе продолжать текущую атаку?
        Атака считается возможной, пока:
        - Есть неприбитые карты на столе, И
        - Атакующий (или другие игроки) могут ещё подкидывать карты.
        """
        if self.game_over or self.attack_finished:
            return False
        if self._get_unbeaten_count() == 0:
            return False
        return self.can_attack_more() or len(self.get_players_who_can_throw()) > 0

    def get_players_who_can_throw(self) -> List[int]:
        """Возвращает список игроков, которые прямо сейчас могут подкинуть карту."""
        if self.game_over or self.attack_finished or not self.attack_in_progress:
            return []
        result = []
        for pid in self.player_ids:
            if pid != self.current_defender and len(self.get_legal_attacks(pid)) > 0:
                result.append(pid)
        return result

    def auto_resolve_if_stuck(self) -> bool:
        """
        Автоматически завершает атаку, если защитник больше не может отбиваться
        (нет легальных ходов или закончились карты).
        Возвращает True, если что-то было сделано.
        """
        if self.game_over or self.attack_finished:
            return False

        defender = self.current_defender
        if not defender:
            return False

        legal_beats = self.get_legal_beats(defender)
        defender_cards = len(self.hands.get(defender, []))

        # Если защитнику нечем бить или у него 0 карт — он вынужден взять
        if len(legal_beats) == 0 or defender_cards == 0:
            if self.table:
                self.take_table(defender)
                return True

        return False

    def can_finish_attack(self) -> bool:
        """
        Можно ли завершить текущий кон?
        Разрешаем закрыть атаку только когда:
        - Все карты на столе отбиты, И
        - Больше никто не может/хочет подкидывать (нет легальных бросков).
        """
        if self.game_over or self.attack_finished:
            return False
        if self._get_unbeaten_count() > 0:
            return False
        # Если ещё есть игроки, которые могут подкинуть — лучше не закрывать волну
        if len(self.get_players_who_can_throw()) > 0:
            return False
        return len(self.table) > 0

    def can_take_table(self) -> bool:
        """Может ли защитник забрать карты со стола?"""
        if self.game_over or self.attack_finished:
            return False
        return len(self.table) > 0 and self._get_unbeaten_count() > 0

    # ====================== ВСПОМОГАТЕЛЬНЫЕ МЕТОДЫ ВАЛИДАЦИИ / СОСТОЯНИЯ ======================

    def _reset_wave_state(self) -> None:
        """Сброс состояния текущей волны атаки (после взятия или 'бито')."""
        self.attack_in_progress = False
        self.attack_finished = False
        self.players_who_threw_this_wave.clear()

    def _cleanup_round(self, to_discard: Optional[List[Card]] = None) -> None:
        """Общий код завершения раунда: сброс стола, добор, проверка конца игры."""
        if to_discard:
            for c in to_discard:
                if c:
                    self.discard_pile.append(c)
        self.table = []
        self._reset_wave_state()
        self.draw_for_players()
        self._check_game_over()

    def get_role(self, player_id: int) -> str:
        """Возвращает роль игрока в текущем раунде."""
        if player_id == self.current_attacker:
            return "attacker"
        if player_id == self.current_defender:
            return "defender"
        if player_id in self.player_ids:
            return "participant"
        return "none"

    def get_allowed_actions(self, player_id: int) -> List[str]:
        """
        Возвращает список разрешённых действий для игрока прямо сейчас.
        Это ключевой метод для клиентской интеграции — клиент не должен
        показывать кнопки/карты, которых нет в этом списке.
        """
        if self.game_over or player_id not in self.player_ids:
            return []

        actions: List[str] = []

        if player_id == self.current_defender:
            # Защитник
            if self.can_take_table():
                actions.append("take_table")
            if self._get_unbeaten_count() > 0:
                # Есть что отбивать и есть легальные биты
                if self.get_legal_beats(player_id):
                    actions.append("beat")
        else:
            # Атакующий или подкидывающий
            legal = self.get_legal_attacks(player_id)
            if legal:
                if not self.attack_in_progress and player_id == self.current_attacker:
                    actions.append("attack")
                elif self.attack_in_progress:
                    actions.append("throw_in")

        # 'Бито' — завершение атаки (обычно доступно атакующему/участникам, когда все отбиты)
        if self.can_finish_attack():
            actions.append("finish_attack")

        # Дедуп
        return list(dict.fromkeys(actions))

    def get_max_attack_cards_remaining(self) -> int:
        """
        Возвращает, сколько карт ещё может подкинуть атакующий в текущем ходу.
        Учитывает:
        - Максимум 6 карт всего на столе
        - Количество карт у защитника
        - Сколько карт уже лежит неприбитых
        """
        if self.game_over or self.attack_finished:
            return 0

        unbeaten = self._get_unbeaten_count()
        defender_cards = len(self.hands.get(self.current_defender, []))

        max_by_defender = defender_cards - unbeaten
        max_by_rules = 6 - len(self.table)

        return max(0, min(max_by_defender, max_by_rules))

    def get_attackable_ranks(self) -> set:
        """Возвращает номиналы, которыми разрешено атаковать в данный момент."""
        if not self.table:
            return set()  # первую карту можно любую (решает клиент/игрок)
        return {c.rank for pair in self.table for c in pair if c is not None}

    def get_current_phase(self) -> str:
        """Возвращает текущую фазу для клиента."""
        if self.game_over:
            return "finished"
        if self.attack_finished:
            return "round_finished"
        if self.table:
            return "defending"
        return "attacking"

    def is_legal_beat(self, player_id: int, attack_card: Card, beat_card: Card) -> bool:
        """Чистая проверка (без изменения состояния), можно ли отбить карту."""
        if self.game_over or self.attack_finished:
            return False
        if player_id != self.current_defender:
            return False

        defender_hand = self.hands.get(player_id, [])
        if beat_card not in defender_hand:
            return False

        # Карта уже на столе?
        all_table_cards = [c for pair in self.table for c in pair if c is not None]
        if beat_card in all_table_cards:
            return False

        # Ищем неприбитую карту
        for atk, bt in self.table:
            if atk == attack_card and bt is None:
                return self._can_beat(atk, beat_card)

        return False

    def is_legal_attack(self, player_id: int, card: Card) -> bool:
        """
        Чистая проверка: можно ли этому игроку подкинуть эту карту прямо сейчас.

        Правила подкидывания в Дураке:
        - Если атака ещё не началась — только текущий атакующий может начать.
        - Если атака уже идёт — любой игрок (кроме защитника) может подкидывать,
          но только карты тех номиналов, что уже на столе.
        - Важно: один и тот же игрок не может подкидывать **второй раз** в одной волне атаки,
          пока круг не завершится (защитник отбил всё или взял карты).
        """
        if self.game_over or self.attack_finished:
            return False

        hand = self.hands.get(player_id, [])
        if card not in hand:
            return False

        if self.get_max_attack_cards_remaining() <= 0:
            return False

        # Игрок уже бросал в этой волне?
        if player_id in self.players_who_threw_this_wave:
            return False

        # Если атака уже идёт — можно подкидывать любому (кроме защитника), у кого есть подходящая карта
        if self.attack_in_progress:
            if player_id == self.current_defender:
                return False
            if self.table:
                table_ranks = {c.rank for pair in self.table for c in pair if c is not None}
                if card.rank not in table_ranks:
                    return False
            return True

        # Атака ещё не началась — только текущий атакующий может начать
        if player_id != self.current_attacker:
            return False

        return True  # первая карта — любая

    def get_legal_attacks(self, player_id: int) -> List[Card]:
        """
        Возвращает карты, которыми игрок может легально подкинуть прямо сейчас.
        Учитывает правило: один игрок не может подкидывать дважды в одной волне атаки.
        """
        if player_id not in self.player_ids:
            return []
        if player_id == self.current_defender:
            return []

        hand = self.hands.get(player_id, [])
        legal = []

        for card in hand:
            if self.is_legal_attack(player_id, card):
                legal.append(card)

        return legal

    def can_player_throw_in(self, player_id: int) -> bool:
        """
        Может ли этот игрок в принципе подкидывать карты в текущий момент атаки?
        Учитывает правило "один игрок — один бросок за волну".
        """
        if self.game_over or self.attack_finished:
            return False
        if player_id in self.players_who_threw_this_wave:
            return False
        if not self.attack_in_progress:
            return player_id == self.current_attacker
        if player_id == self.current_defender:
            return False
        return len(self.get_legal_attacks(player_id)) > 0

    def get_legal_beats(self, player_id: int) -> List[tuple[Card, Card]]:
        """
        Возвращает список пар (attack_card, beat_card),
        которые защитник может легально использовать для отбива прямо сейчас.
        """
        if player_id != self.current_defender:
            return []

        defender_hand = self.hands.get(player_id, [])
        legal_beats = []

        for atk, bt in self.table:
            if bt is not None:
                continue  # уже отбито

            for beat_card in defender_hand:
                if self.is_legal_beat(player_id, atk, beat_card):
                    legal_beats.append((atk, beat_card))

        return legal_beats

    def _get_unbeaten_count(self) -> int:
        """Сколько карт на столе ещё не отбиты."""
        return sum(1 for _, bt in self.table if bt is None)

    def _check_game_over(self):
        """Проверяет, не закончилась ли игра (у кого-то 0 карт после добора)."""
        if self.game_over:
            return

        for pid in self.player_ids:
            if len(self.hands.get(pid, [])) == 0:
                self.game_over = True
                self.winner = pid
                return

    def __repr__(self):
        return f"<DurakGame players={len(self.player_ids)} trump={self.trump_suit}>"

    def get_game_state_summary(self) -> dict:
        """Возвращает удобный словарь состояния игры (для логов и отладки)."""
        return {
            "attacker": self.current_attacker,
            "defender": self.current_defender,
            "trump": str(self.trump_suit) if self.trump_suit else None,
            "table_size": len(self.table),
            "unbeaten": self._get_unbeaten_count(),
            "phase": self.get_current_phase(),
            "can_attack_more": self.can_attack_more(),
            "max_attack_remaining": self.get_max_attack_cards_remaining(),
        }

    def get_full_game_state(self, viewer_id: Optional[int] = None) -> dict:
        """
        Возвращает полное состояние игры для клиента.
        Если указан viewer_id — его рука отдаётся полностью, остальные маскируются.
        """
        hands = {}
        for pid in self.player_ids:
            if viewer_id is not None and pid == viewer_id:
                hands[pid] = [str(c) for c in self.hands.get(pid, [])]
            else:
                hands[pid] = len(self.hands.get(pid, []))

        table = []
        for atk, bt in self.table:
            table.append({
                "attack": str(atk),
                "beat": str(bt) if bt else None
            })

        legal_attacks = []
        legal_beats = []

        if viewer_id is not None:
            legal_attacks = [str(c) for c in self.get_legal_attacks(viewer_id)]
            legal_beats = [
                {"attack": str(atk), "beat": str(beat)}
                for atk, beat in self.get_legal_beats(viewer_id)
            ]

        players_who_can_throw = []
        if self.attack_in_progress:
            for pid in self.player_ids:
                if pid != self.current_defender and len(self.get_legal_attacks(pid)) > 0:
                    players_who_can_throw.append(pid)

        role = None
        allowed_actions: List[str] = []
        if viewer_id is not None:
            role = self.get_role(viewer_id)
            allowed_actions = self.get_allowed_actions(viewer_id)

        return {
            "players": self.player_ids,
            "attacker": self.current_attacker,
            "defender": self.current_defender,
            "trump_suit": str(self.trump_suit) if self.trump_suit else None,
            "game_type": self.game_type,
            "hands": hands,
            "table": table,
            "discard_count": len(self.discard_pile),
            "deck_remaining": self.deck.remaining_cards(),
            "phase": self.get_current_phase(),
            "attack_in_progress": self.attack_in_progress,
            "attack_finished": self.attack_finished,
            "game_over": self.game_over,
            "winner": self.winner,
            "role": role,
            "allowed_actions": allowed_actions,
            "legal_attacks": legal_attacks,
            "legal_beats": legal_beats,
            "players_who_can_throw_in": players_who_can_throw,
            "players_who_already_threw_this_wave": list(self.players_who_threw_this_wave),
            "can_attack_more": self.can_attack_more(),
            "can_finish_attack": self.can_finish_attack(),
            "can_take_table": self.can_take_table(),
            "max_attack_cards_remaining": self.get_max_attack_cards_remaining(),
        }


if __name__ == "__main__":
    print("=== Тест Deck ===")
    deck = Deck(36)
    print(f"Создана колода из {deck.size} карт")

    hands = deck.deal(4)
    print(f"Козырь: {deck.trump_suit}")

    for i, hand in enumerate(hands):
        print(f"Игрок {i+1}: {[str(c) for c in hand]}")

    print(f"Осталось в колоде после раздачи: {deck.remaining_cards()}")

    first_attacker = deck.determine_first_attacker(hands)
    print(f"Первый атакующий: Игрок {first_attacker + 1}")

    print("\n=== Тест DurakGame ===")
    players = [101, 102, 103, 104]
    game = DurakGame(players, deck_size=36)
    game.start_game()

    print(f"Игра началась. Атакует: {game.current_attacker}, Защищается: {game.current_defender}")
    print(f"Козырь: {game.trump_suit}")

    for pid in players:
        print(f"  Игрок {pid}: {[str(c) for c in game.get_hand(pid)]}")

    # Демонстрация валидации
    attacker = game.current_attacker
    hand = game.get_hand(attacker)
    if hand:
        card_to_play = hand[0]
        print(f"\nМожно ли атаковать {card_to_play}? -> {game.is_legal_attack(attacker, card_to_play)}")

        success = game.attack(attacker, card_to_play)
        print(f"Игрок {attacker} атакует картой {card_to_play} -> {'успешно' if success else 'неуспешно'}")
        print(f"Стол сейчас: {game.get_table()}")

        print(f"Можно ли ещё атаковать? -> {game.can_attack_more()}")
        print(f"Максимум карт можно ещё подкинуть: {game.get_max_attack_cards_remaining()}")
        print(f"Легальные атаки: {[str(c) for c in game.get_legal_attacks(attacker)]}")
        print(f"Текущая фаза: {game.get_current_phase()}")
