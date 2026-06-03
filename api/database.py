# database.py — thin re-export shim
# Все символы по-прежнему доступны через `from api.database import ...`
# Реальный код живёт в api/db/*.py

from api.db.connection import (
    get_connection,
    init_db,
    is_test_user_id,
    get_protected_user_ids,
    admin_purge_test_players,
    DATABASE_URL,
    TEST_PLAYER_IDS,
    TEST_ID_RANGE,
)

from api.db.scores import (
    save_score,
    get_user_stats,
    get_leaderboard,
)

from api.db.contests import (
    create_contest,
    get_active_contests,
    get_contest,
    finish_contest,
    mark_prize_sent,
    cancel_contest,
)

from api.db.wallet import (
    get_wallet,
    topup_wallet,
    spend_wallet,
    get_wallet_transactions,
)

from api.db.energy import (
    ENERGY_MAX,
    ENERGY_REGEN_MS,
    get_energy,
    spend_energy,
    admin_adjust_energy,
)

from api.db.cases import (
    CASE_PRICE,
    CASE_REWARD_DEDUP_SEC,
    CASE_VALUABLE_CHANCE_DEFAULT,
    CASE_NFT_IN_VALUABLE_SHARE,
    CASE_VALUABLE_COOLDOWN_MIN_DEFAULT,
    get_case_settings,
    save_case_settings,
    get_case_valuable_cooldown_status,
    get_recent_case_reward,
    grant_case_reward,
    confirm_case_reward,
)

from api.db.referrals import (
    REFERRAL_STARS,
    REFERRAL_ENERGY,
    REFERRAL_GAMES_NEEDED,
    FRAUD_DAILY_LIMIT,
    FRAUD_INACTIVE_RATIO,
    register_referral,
    accept_referral_policy,
    get_referral_by_invitee,
    get_invitee_total_games,
    try_grant_referral_reward,
    claim_referral_reward,
    get_referral_stats,
    is_already_referred,
    check_fraud_daily_flood,
    check_fraud_inactive_ratio,
)

from api.db.admin import (
    get_user_flags,
    admin_delete_player,
    admin_get_all_players,
    admin_ensure_self,
    admin_get_player,
    admin_adjust_wallet,
    admin_adjust_score,
    admin_set_blocked,
    admin_set_ref_disabled,
    admin_get_summary_stats,
    admin_reset_player_scores,
    admin_reset_all_scores,
    admin_set_energy,
    admin_set_all_energy,
    admin_zero_wallet,
    admin_zero_all_wallets,
)

from api.db.durak import (
    create_durak_lobby,
    get_active_durak_lobbies,
    join_durak_lobby,
    get_lobby_players,
    finish_durak_lobby,
    save_durak_game_state,
    load_durak_game_state,
    delete_durak_game_state,
    list_active_durak_game_lobbies,
    leave_durak_lobby,
    update_lobby_settings,
    set_player_ready,
    is_user_in_active_lobby,
    start_durak_game,
    get_durak_lobby_by_id,
    save_durak_game_history,
    get_durak_history,
    get_durak_ratings,
    get_durak_user_stats,
    admin_reset_durak_all,
    admin_reset_durak_player,
    ban_durak_user,
    is_durak_banned,
)

__all__ = [
    # connection
    "get_connection", "init_db", "is_test_user_id", "get_protected_user_ids",
    "admin_purge_test_players", "DATABASE_URL", "TEST_PLAYER_IDS", "TEST_ID_RANGE",
    # scores
    "save_score", "get_user_stats", "get_leaderboard",
    # contests
    "create_contest", "get_active_contests", "get_contest", "finish_contest",
    "mark_prize_sent", "cancel_contest",
    # wallet
    "get_wallet", "topup_wallet", "spend_wallet", "get_wallet_transactions",
    # energy
    "ENERGY_MAX", "ENERGY_REGEN_MS", "get_energy", "spend_energy", "admin_adjust_energy",
    # cases
    "CASE_PRICE", "CASE_REWARD_DEDUP_SEC", "CASE_VALUABLE_CHANCE_DEFAULT",
    "CASE_NFT_IN_VALUABLE_SHARE", "CASE_VALUABLE_COOLDOWN_MIN_DEFAULT",
    "get_case_settings", "save_case_settings", "get_case_valuable_cooldown_status",
    "get_recent_case_reward", "grant_case_reward", "confirm_case_reward",
    # referrals
    "REFERRAL_STARS", "REFERRAL_ENERGY", "REFERRAL_GAMES_NEEDED",
    "FRAUD_DAILY_LIMIT", "FRAUD_INACTIVE_RATIO",
    "register_referral", "accept_referral_policy", "get_referral_by_invitee",
    "get_invitee_total_games", "try_grant_referral_reward", "claim_referral_reward",
    "get_referral_stats", "is_already_referred",
    "check_fraud_daily_flood", "check_fraud_inactive_ratio",
    # admin
    "get_user_flags", "admin_delete_player", "admin_get_all_players",
    "admin_ensure_self", "admin_get_player", "admin_adjust_wallet",
    "admin_adjust_score", "admin_set_blocked", "admin_set_ref_disabled",
    "admin_get_summary_stats",
    "admin_reset_player_scores", "admin_reset_all_scores",
    "admin_set_energy", "admin_set_all_energy",
    "admin_zero_wallet", "admin_zero_all_wallets",
    # durak
    "create_durak_lobby", "get_active_durak_lobbies", "join_durak_lobby",
    "get_lobby_players", "finish_durak_lobby",
    "save_durak_game_state", "load_durak_game_state", "delete_durak_game_state",
    "list_active_durak_game_lobbies",
    "leave_durak_lobby", "update_lobby_settings",
    "set_player_ready", "is_user_in_active_lobby", "start_durak_game",
    "get_durak_lobby_by_id", "save_durak_game_history", "get_durak_history",
    "get_durak_ratings", "get_durak_user_stats",
    "admin_reset_durak_all", "admin_reset_durak_player",
    "ban_durak_user", "is_durak_banned",
]
