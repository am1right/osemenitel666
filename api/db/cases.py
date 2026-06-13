import json
import random
from typing import Dict, Any, List, Optional

from api.db.connection import get_connection, _cursor
from api.db.wallet import topup_wallet
from api.db.chent import topup_chent

CASE_PRICE                         = 500
CASE_REWARD_DEDUP_SEC              = 20
CASE_VALUABLE_CHANCE_DEFAULT       = 0.4
CASE_NFT_IN_VALUABLE_SHARE         = 0.45
CASE_VALUABLE_COOLDOWN_MIN_DEFAULT = 60
CASE_NFT_CHANCE_DEFAULT            = 0.18   # отдельный шанс именно NFT (0..1)

# Цены кейсов в Choin (1 wallet-star = 10 Choin, см. CHOIN_RATE на фронте)
CASE_PRICES = {1: 250, 2: 400, 3: 5000}

# Дневной лимит NFT-дропов из кейса 3 и порог алерта баланса бота (Stars)
CASE_NFT_DAILY_LIMIT  = 5
CASE_BOT_STARS_ALERT_THRESHOLD = 500


def get_nft_drop_count_today() -> int:
    conn = get_connection()
    cur  = _cursor(conn)
    cur.execute(
        "SELECT COUNT(*) AS cnt FROM case_rewards "
        "WHERE case_id = 3 AND reward_json::jsonb ->> 'type' = 'nft' "
        "AND created_at >= date_trunc('day', NOW())"
    )
    row = cur.fetchone()
    cur.close()
    conn.close()
    return int(row["cnt"]) if row else 0


# ── Таблицы дропа кейсов 1 и 2 (УТВЕРЖДЕНО, не менять без «да») ──────
# Кейс 1 — цена 25⭐ (250 Choin)
CASE1_TABLE = [
    (0.25, {"type": "chent", "amount": 500,  "title": "+500 chent"}),
    (0.30, {"type": "choin", "amount": 100,  "title": "+100 choin"}),
    (0.30, {"type": "tg_gift", "stars_value": 15, "title": "Подарок 15⭐"}),
    (0.10, {"type": "tg_gift", "stars_value": 25, "title": "Подарок 25⭐"}),
    (0.05, {"type": "tg_gift", "stars_value": 50, "title": "Подарок 50⭐"}),
]

# Кейс 2 — цена 40⭐ (400 Choin)
CASE2_TABLE = [
    (0.20, {"type": "chent", "amount": 800,  "title": "+800 chent"}),
    (0.25, {"type": "choin", "amount": 200,  "title": "+200 choin"}),
    (0.25, {"type": "tg_gift", "stars_value": 15,  "title": "Подарок 15⭐"}),
    (0.16, {"type": "tg_gift", "stars_value": 25,  "title": "Подарок 25⭐"}),
    (0.10, {"type": "tg_gift", "stars_value": 50,  "title": "Подарок 50⭐"}),
    (0.04, {"type": "tg_gift", "stars_value": 100, "title": "Подарок 100⭐"}),
]


def _weighted_pick(table):
    r = random.random()
    acc = 0.0
    for chance, pick in table:
        acc += chance
        if r < acc:
            return dict(pick)
    return dict(table[-1][1])


def _apply_simple_pick(user_id: int, first_name: str, pick: Dict[str, Any]) -> Dict[str, Any]:
    reward_type = pick["type"]
    title       = pick["title"]
    if reward_type == "chent":
        amount = pick["amount"]
        wallet = topup_chent(user_id, first_name or "Игрок", amount, description="Награда из кейса")
        return {"type": "chent", "amount": amount, "title": title, "balance": wallet["balance"]}
    if reward_type == "choin":
        amount = pick["amount"]
        # Choin хранится как wallet-stars (1 wallet = 10 choin)
        wallet = topup_wallet(user_id, first_name or "Игрок", amount // 10, description="Награда из кейса")
        return {"type": "choin", "amount": amount, "title": title, "balance": wallet["balance"]}
    if reward_type == "tg_gift":
        return {"type": "tg_gift", "stars_value": pick["stars_value"], "title": title}
    raise ValueError(f"unknown reward type: {reward_type}")


def grant_case1_reward(user_id: int, first_name: str = "") -> Dict[str, Any]:
    pick   = _weighted_pick(CASE1_TABLE)
    reward = _apply_simple_pick(user_id, first_name, pick)
    reward["tier"] = "common"
    conn = get_connection()
    cur  = _cursor(conn)
    cur.execute(
        'INSERT INTO case_rewards (user_id, reward_json, is_valuable, case_id) VALUES (%s, %s, 0, 1)',
        (user_id, json.dumps(reward, ensure_ascii=False)),
    )
    conn.commit()
    cur.close()
    conn.close()
    return reward


def grant_case2_reward(user_id: int, first_name: str = "") -> Dict[str, Any]:
    pick   = _weighted_pick(CASE2_TABLE)
    reward = _apply_simple_pick(user_id, first_name, pick)
    reward["tier"] = "common"
    conn = get_connection()
    cur  = _cursor(conn)
    cur.execute(
        'INSERT INTO case_rewards (user_id, reward_json, is_valuable, case_id) VALUES (%s, %s, 0, 2)',
        (user_id, json.dumps(reward, ensure_ascii=False)),
    )
    conn.commit()
    cur.close()
    conn.close()
    return reward


def get_case_settings() -> Dict[str, Any]:
    conn = get_connection()
    cur  = _cursor(conn)
    cur.execute("SELECT nft_gifts, valuable_chance, valuable_cooldown_min, nft_chance FROM case_settings WHERE id = 1")
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row:
        return {
            "nft_gifts":             [],
            "valuable_chance":       CASE_VALUABLE_CHANCE_DEFAULT,
            "valuable_cooldown_min": CASE_VALUABLE_COOLDOWN_MIN_DEFAULT,
            "nft_chance":            CASE_NFT_CHANCE_DEFAULT,
        }
    try:
        gifts = json.loads(row["nft_gifts"] or "[]")
    except json.JSONDecodeError:
        gifts = []
    if not isinstance(gifts, list):
        gifts = []
    gifts = [u.strip() for u in gifts if isinstance(u, str) and u.strip().startswith("http")]
    chance = float(row["valuable_chance"] if row["valuable_chance"] is not None else CASE_VALUABLE_CHANCE_DEFAULT)
    chance = max(0.05, min(0.95, chance))
    try:
        cooldown_min = int(row["valuable_cooldown_min"])
    except (KeyError, TypeError, ValueError):
        cooldown_min = CASE_VALUABLE_COOLDOWN_MIN_DEFAULT
    cooldown_min = max(5, min(24 * 60, cooldown_min))
    try:
        nft_chance = float(row["nft_chance"] if row["nft_chance"] is not None else CASE_NFT_CHANCE_DEFAULT)
    except (KeyError, TypeError, ValueError):
        nft_chance = CASE_NFT_CHANCE_DEFAULT
    nft_chance = max(0.0, min(1.0, nft_chance))   # допускаем 0%..100%
    return {"nft_gifts": gifts, "valuable_chance": chance, "valuable_cooldown_min": cooldown_min, "nft_chance": nft_chance}


def save_case_settings(
    nft_gifts: List[str],
    valuable_chance: float = CASE_VALUABLE_CHANCE_DEFAULT,
    valuable_cooldown_min: int = CASE_VALUABLE_COOLDOWN_MIN_DEFAULT,
    nft_chance: float = CASE_NFT_CHANCE_DEFAULT,
) -> Dict[str, Any]:
    cleaned = []
    for url in nft_gifts:
        if not isinstance(url, str):
            continue
        u = url.strip()
        if u.startswith("http") and u not in cleaned:
            cleaned.append(u)
    chance       = max(0.05, min(0.95, float(valuable_chance)))
    cooldown_min = max(5, min(24 * 60, int(valuable_cooldown_min)))
    nft_ch       = max(0.0, min(1.0, float(nft_chance)))
    conn = get_connection()
    cur  = _cursor(conn)
    cur.execute('''
        INSERT INTO case_settings (id, nft_gifts, valuable_chance, valuable_cooldown_min, nft_chance, updated_at)
        VALUES (1, %s, %s, %s, %s, NOW())
        ON CONFLICT (id) DO UPDATE SET
            nft_gifts             = EXCLUDED.nft_gifts,
            valuable_chance       = EXCLUDED.valuable_chance,
            valuable_cooldown_min = EXCLUDED.valuable_cooldown_min,
            nft_chance            = EXCLUDED.nft_chance,
            updated_at            = NOW()
    ''', (json.dumps(cleaned, ensure_ascii=False), chance, cooldown_min, nft_ch))
    conn.commit()
    cur.close()
    conn.close()
    return {"nft_gifts": cleaned, "valuable_chance": chance, "valuable_cooldown_min": cooldown_min, "nft_chance": nft_ch}


def _roll_common_stars() -> int:
    r = random.random()
    if r < 0.04: return 250
    if r < 0.14: return random.randint(100, 249)
    if r < 0.38: return random.randint(40, 99)
    return random.randint(5, 39)


def _pick_common_reward() -> Dict[str, Any]:
    amount = _roll_common_stars()
    title  = f"Джекпот +{amount * 10} choin" if amount >= 250 else f"+{amount * 10} choin"
    return {"type": "stars", "amount": amount, "title": title}


def _pick_valuable_reward(nft_gifts: List[str], allow_nft: bool = True) -> Dict[str, Any]:
    if allow_nft and nft_gifts and random.random() < CASE_NFT_IN_VALUABLE_SHARE:
        url = random.choice(nft_gifts)
        return {"type": "nft", "gift_url": url, "amount": 0, "title": "NFT-подарок!"}
    if random.random() < 0.5:
        amount = random.randint(100, 249)
        return {"type": "stars", "amount": amount, "title": f"+{amount * 10} choin"}
    return {"type": "stars", "amount": 250, "title": f"Джекпот +{250 * 10} choin"}


def _is_global_valuable_on_cooldown(cooldown_sec: int) -> bool:
    if cooldown_sec <= 0:
        return False
    conn = get_connection()
    cur  = _cursor(conn)
    cur.execute(
        "SELECT 1 FROM case_rewards WHERE is_valuable = 1 AND created_at >= NOW() - (%s * INTERVAL '1 second') LIMIT 1",
        (cooldown_sec,),
    )
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row is not None


def get_case_valuable_cooldown_status() -> Dict[str, Any]:
    settings     = get_case_settings()
    cooldown_sec = settings["valuable_cooldown_min"] * 60
    base = {"on_cooldown": False, "cooldown_min": settings["valuable_cooldown_min"], "seconds_left": 0}
    if not _is_global_valuable_on_cooldown(cooldown_sec):
        return base
    conn = get_connection()
    cur  = _cursor(conn)
    cur.execute(
        "SELECT GREATEST(0, EXTRACT(EPOCH FROM (MAX(created_at) + (%s * INTERVAL '1 second') - NOW()))::INTEGER) AS seconds_left FROM case_rewards WHERE is_valuable = 1",
        (cooldown_sec,),
    )
    row = cur.fetchone()
    cur.close()
    conn.close()
    seconds_left = int(row["seconds_left"]) if row and row["seconds_left"] is not None else 0
    return {"on_cooldown": True, "cooldown_min": settings["valuable_cooldown_min"], "seconds_left": seconds_left}


def get_recent_case_reward(user_id: int, within_sec: int = CASE_REWARD_DEDUP_SEC) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    cur  = _cursor(conn)
    cur.execute(
        "SELECT reward_json FROM case_rewards WHERE user_id = %s AND created_at >= NOW() - (%s * INTERVAL '1 second') ORDER BY id DESC LIMIT 1",
        (user_id, within_sec),
    )
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row:
        return None
    return json.loads(row["reward_json"])


def _save_case_reward(cur, user_id: int, reward: Dict[str, Any]) -> None:
    is_valuable = 1 if reward.get("tier") == "valuable" else 0
    cur.execute(
        'INSERT INTO case_rewards (user_id, reward_json, is_valuable) VALUES (%s, %s, %s)',
        (user_id, json.dumps(reward, ensure_ascii=False), is_valuable),
    )


def _apply_case_pick(user_id: int, first_name: str, pick: Dict[str, Any]) -> Dict[str, Any]:
    reward_type = pick["type"]
    amount      = pick.get("amount", 0)
    title       = pick["title"]
    if reward_type == "nft":
        return {"type": "nft", "gift_url": pick["gift_url"], "amount": 0, "title": title}
    wallet = topup_wallet(user_id, first_name or "Игрок", amount, description="Награда из кейса")
    return {"type": "stars", "amount": amount, "title": title, "balance": wallet["balance"]}


def grant_case_reward(user_id: int, first_name: str = "") -> Dict[str, Any]:
    settings         = get_case_settings()
    nft_gifts        = settings["nft_gifts"]
    valuable_chance  = settings["valuable_chance"]
    nft_chance       = settings.get("nft_chance", CASE_NFT_CHANCE_DEFAULT)
    cooldown_sec     = settings["valuable_cooldown_min"] * 60

    # 1) NFT решается ОТДЕЛЬНЫМ шансом и ни на что больше не влияет.
    #    Дневной лимит NFT-дропов: при достижении лимита слот NFT замещается 5000 Choin.
    if nft_gifts and random.random() < nft_chance and get_nft_drop_count_today() < CASE_NFT_DAILY_LIMIT:
        url  = random.choice(nft_gifts)
        pick = {"type": "nft", "gift_url": url, "amount": 0, "title": "NFT-подарок!"}
        tier = "valuable"
    else:
        # 2) Остальные награды — по своей логике (без NFT, он уже разыгран выше).
        valuable_blocked = _is_global_valuable_on_cooldown(cooldown_sec)
        roll_valuable    = random.random() < valuable_chance and not valuable_blocked
        if roll_valuable:
            pick = _pick_valuable_reward(nft_gifts, allow_nft=False)
            tier = "valuable"
        else:
            pick = _pick_common_reward()
            tier = "common"
    reward         = _apply_case_pick(user_id, first_name, pick)
    reward["tier"] = tier
    conn = get_connection()
    cur  = _cursor(conn)
    _save_case_reward(cur, user_id, reward)
    conn.commit()
    cur.close()
    conn.close()
    return reward


def confirm_case_reward(user_id: int, first_name: str = "") -> Dict[str, Any]:
    recent = get_recent_case_reward(user_id)
    if recent:
        return recent
    return grant_case_reward(user_id, first_name)
