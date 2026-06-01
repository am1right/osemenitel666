"""Test that after take_table, the new attacker can start a new attack."""
from api.durak_game import DurakGame, Card, Rank, Suit

players = [101, 102, 103]
g = DurakGame(players, 36)
g.start_game()

print("Initial attacker:", g.current_attacker, "defender:", g.current_defender)

# Force a simple situation: attacker plays one card
att = g.current_attacker
hand = g.get_hand(att)
if not hand:
    print("No cards in hand after deal")
    exit(1)

card = hand[0]
print(f"Attacker {att} plays first card (rank={card.rank.value})")
ok = g.attack(att, card)
print("First attack success:", ok)
print("Table size:", len(g.table))
print("attack_in_progress:", g.attack_in_progress, "attack_finished:", g.attack_finished)

# Defender takes
defender = g.current_defender
print(f"Defender {defender} takes the table")
took = g.take_table(defender)
print("take_table success:", took)
print("New attacker:", g.current_attacker, "new defender:", g.current_defender)
print("After take - attack_in_progress:", g.attack_in_progress, "attack_finished:", g.attack_finished)

# Check if new attacker can legally attack
new_att = g.current_attacker
new_hand = g.get_hand(new_att)
print(f"New attacker {new_att} hand size: {len(new_hand)}")

if new_hand:
    test_card = new_hand[0]
    legal = g.is_legal_attack(new_att, test_card)
    allowed = g.get_allowed_actions(new_att)
    print(f"Can new attacker play a card? is_legal_attack={legal}")
    print(f"allowed_actions for new attacker: {allowed}")

    if "attack" in allowed or legal:
        print("SUCCESS: New attacker can start new round after take!")
    else:
        print("BUG: New attacker still cannot attack after take")
else:
    print("New attacker has no cards")
