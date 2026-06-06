import json
import random
from typing import Dict, Any, List, Optional

from api.db.connection import get_connection, _cursor
from api.db.wallet import topup_wallet
from api.db.energy import admin_adjust_energy

CASE_PRICE                         = 600
CASE_REWARD_DEDUP_SEC              = 20
CASE_VALUABLE_CHANCE_DEFAULT       = 0.4
CASE_NFT_IN_VALUABLE_SHARE         = 0.45
CASE_VALUABLE_COOLDOWN_MIN_DEFAULT = 60
CASE_NFT_CHANCE_DEFAULT            = 0.18   # отдельный шанс именно NFT (0..1)


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
    if r < 0.04: return 200
    if r < 0.14: return random.randint(100, 199)
    if r < 0.38: return random.randint(40, 99)
    return random.randint(5, 39)


def _pick_common_reward() -> Dict[str, Any]:
    if random.random() < 0.5:
        amount = random.randint(1, 20)
        return {"type": "energy", "amount": amount, "title": f"+{amount} энергии"}
    amount = _roll_common_stars()
    title  = "Джекпот +200 ⭐" if amount >= 200 else f"+{amount} ⭐"
    return {"type": "stars", "amount": amount, "title": title}


def _pick_valuable_reward(nft_gifts: List[str], allow_nft: bool = True) -> Dict[str, Any]:
    if allow_nft and nft_gifts and random.random() < CASE_NFT_IN_VALUABLE_SHARE:
        url = random.choice(nft_gifts)
        return {"type": "nft", "gift_url": url, "amount": 0, "title": "NFT-подарок!"}
    roll = random.random()
    if roll < 0.34:
        amount = random.randint(15, 20)
        return {"type": "energy", "amount": amount, "title": f"+{amount} энергии"}
    if roll < 0.67:
        amount = random.randint(100, 199)
        return {"type": "stars", "amount": amount, "title": f"+{amount} ⭐"}
    return {"type": "stars", "amount": 200, "title": "Джекпот +200 ⭐"}


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
    if reward_type == "energy":
        result = admin_adjust_energy(user_id, amount)
        return {"type": "energy", "amount": amount, "title": title, "energy": result["amount"]}
    wallet = topup_wallet(user_id, first_name or "Игрок", amount, description="Награда из кейса")
    return {"type": "stars", "amount": amount, "title": title, "balance": wallet["balance"]}


def grant_case_reward(user_id: int, first_name: str = "") -> Dict[str, Any]:
    settings         = get_case_settings()
    nft_gifts        = settings["nft_gifts"]
    valuable_chance  = settings["valuable_chance"]
    nft_chance       = settings.get("nft_chance", CASE_NFT_CHANCE_DEFAULT)
    cooldown_sec     = settings["valuable_cooldown_min"] * 60

    # 1) NFT решается ОТДЕЛЬНЫМ шансом и ни на что больше не влияет.
    if nft_gifts and random.random() < nft_chance:
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
