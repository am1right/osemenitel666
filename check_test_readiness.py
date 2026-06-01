"""Quick readiness check for Durak testing."""
import sys
print("Python:", sys.version.split()[0])

errors = []

try:
    from api.durak_game import DurakGame, Card
    print("[OK] durak_game imported")
except Exception as e:
    errors.append(f"durak_game import: {e}")

try:
    from api.durak_routes import router, active_games, perform_game_action, get_game_state
    print("[OK] durak_routes imported (endpoints exist)")
except Exception as e:
    errors.append(f"durak_routes import: {e}")

if not errors:
    try:
        g = DurakGame([101, 102], 36)
        g.start_game()
        st = g.get_full_game_state(101)
        print("[OK] DurakGame creation + start + get_full_game_state works")
        print("     Sample allowed_actions for player 101:", st.get("allowed_actions"))
        print("     Role:", st.get("role"))
    except Exception as e:
        errors.append(f"Game smoke test failed: {e}")

if errors:
    print("\n=== PROBLEMS FOUND ===")
    for e in errors:
        print(" -", e)
    sys.exit(1)
else:
    print("\n=== BACKEND LOOKS READY FOR TESTING ===")
    sys.exit(0)
