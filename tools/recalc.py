#!/usr/bin/env python3.11
from __future__ import annotations

import argparse
import asyncio
import math
import os
import sys
from collections.abc import Awaitable
from collections.abc import Iterator
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Any
from typing import TypeVar

import databases
from akatsuki_pp_py import Beatmap
from akatsuki_pp_py import Calculator
from redis import asyncio as aioredis

sys.path.insert(0, os.path.abspath(os.pardir))
os.chdir(os.path.abspath(os.pardir))

try:
    from app.constants.privileges import Privileges
    from app.constants.mods import Mods
    from app.constants.gamemodes import GameMode
    from app.objects.beatmap import ensure_local_osu_file
    import app.settings
    import app.state.services
except ModuleNotFoundError:
    print("\x1b[;91mMust run from tools/ directory\x1b[m")
    raise

T = TypeVar("T")


DEBUG = False
BEATMAPS_PATH = Path.cwd() / ".data/osu"


@dataclass
class Context:
    database: databases.Database
    redis: aioredis.Redis
    beatmaps: dict[int, Beatmap] = field(default_factory=dict)


def divide_chunks(values: list[T], n: int) -> Iterator[list[T]]:
    for i in range(0, len(values), n):
        yield values[i : i + n]


async def recalculate_score(
    score: dict[str, Any],
    beatmap_path: Path,
    ctx: Context,
) -> None:
    beatmap = ctx.beatmaps.get(score["map_id"])
    if beatmap is None:
        beatmap = Beatmap(path=str(beatmap_path))
        ctx.beatmaps[score["map_id"]] = beatmap

    calculator = Calculator(
        mode=GameMode(score["mode"]).as_vanilla,
        mods=score["mods"],
        combo=score["max_combo"],
        n_geki=score["ngeki"],  # Mania 320s
        n300=score["n300"],
        n_katu=score["nkatu"],  # Mania 200s, Catch tiny droplets
        n100=score["n100"],
        n50=score["n50"],
        n_misses=score["nmiss"],
    )
    attrs = calculator.performance(beatmap)

    new_pp: float = attrs.pp
    if math.isnan(new_pp) or math.isinf(new_pp):
        new_pp = 0.0

    new_pp = min(new_pp, 9999.999)

    await ctx.database.execute(
        "UPDATE scores SET pp = :new_pp WHERE id = :id",
        {"new_pp": new_pp, "id": score["id"]},
    )

    if DEBUG:
        print(
            f"Recalculated score ID {score['id']} ({score['pp']:.3f}pp -> {new_pp:.3f}pp)",
        )


async def process_score_chunk(
    chunk: list[dict[str, Any]],
    ctx: Context,
) -> None:
    tasks: list[Awaitable[None]] = []
    for score in chunk:
        beatmap_path = BEATMAPS_PATH / f"{score['map_id']}.osu"
        await ensure_local_osu_file(beatmap_path, score["map_id"], score["map_md5"])

        tasks.append(recalculate_score(score, beatmap_path, ctx))

    await asyncio.gather(*tasks)


async def recalculate_user(
    id: int,
    game_mode: GameMode,
    ctx: Context,
) -> None:
    best_scores = await ctx.database.fetch_all(
        "SELECT s.pp, s.acc FROM scores s "
        "INNER JOIN maps m ON s.map_md5 = m.md5 "
        "WHERE s.userid = :user_id AND s.mode = :mode "
        "AND s.status = 2 AND m.status IN (2, 3) "  # ranked, approved
        "ORDER BY s.pp DESC",
        {"user_id": id, "mode": game_mode},
    )

    total_scores = len(best_scores)
    if not total_scores:
        return

    top_100_pp = best_scores[:100]

    # calculate new total weighted accuracy
    weighted_acc = sum(row["acc"] * 0.95**i for i, row in enumerate(top_100_pp))
    bonus_acc = 100.0 / (20 * (1 - 0.95**total_scores))
    acc = (weighted_acc * bonus_acc) / 100

    # calculate new total weighted pp
    weighted_pp = sum(row["pp"] * 0.95**i for i, row in enumerate(top_100_pp))
    bonus_pp = 416.6667 * (1 - 0.9994**total_scores)
    pp = round(weighted_pp + bonus_pp)

    await ctx.database.execute(
        "UPDATE stats SET pp = :pp, acc = :acc WHERE id = :id AND mode = :mode",
        {"pp": pp, "acc": acc, "id": id, "mode": game_mode},
    )

    user_info = await ctx.database.fetch_one(
        "SELECT country, priv FROM users WHERE id = :id",
        {"id": id},
    )
    if user_info is None:
        raise Exception(f"Unknown user ID {id}?")

    if user_info["priv"] & Privileges.UNRESTRICTED:
        await ctx.redis.zadd(
            f"bancho:leaderboard:{game_mode.value}",
            {str(id): pp},
        )

        await ctx.redis.zadd(
            f"bancho:leaderboard:{game_mode.value}:{user_info['country']}",
            {str(id): pp},
        )

    if DEBUG:
        print(f"Recalculated user ID {id} ({pp:.3f}pp, {acc:.3f}%)")


async def process_user_chunk(
    chunk: list[int],
    game_mode: GameMode,
    ctx: Context,
) -> None:
    tasks: list[Awaitable[None]] = []
    for id in chunk:
        tasks.append(recalculate_user(id, game_mode, ctx))

    await asyncio.gather(*tasks)


async def recalculate_mode_users(mode: GameMode, ctx: Context) -> None:
    user_ids = [
        row["id"] for row in await ctx.database.fetch_all("SELECT id FROM users")
    ]

    for id_chunk in divide_chunks(user_ids, 100):
        await process_user_chunk(id_chunk, mode, ctx)


async def recalculate_mode_scores(mode: GameMode, ctx: Context) -> None:
    scores = [
        dict(row)
        for row in await ctx.database.fetch_all(
            """\
            SELECT scores.id, scores.mode, scores.mods, scores.map_md5,
              scores.pp, scores.acc, scores.max_combo,
              scores.ngeki, scores.n300, scores.nkatu, scores.n100, scores.n50, scores.nmiss,
              maps.id as `map_id`
            FROM scores
            INNER JOIN maps ON scores.map_md5 = maps.md5
            WHERE scores.status > 0
              AND scores.mode = :mode
            ORDER BY scores.pp DESC
            """,
            {"mode": mode},
        )
    ]

    for score_chunk in divide_chunks(scores, 100):
        await process_score_chunk(score_chunk, ctx)


async def recalculate_score_status(mode: GameMode, ctx: Context) -> None:
    pairs = await ctx.database.fetch_all(
        "SELECT DISTINCT map_md5, userid, mode FROM scores WHERE status > 0 AND mode = :mode",
        {"mode": mode}
    )

    for pair in pairs:
        scores = await ctx.database.fetch_all(
            "SELECT id, status, pp, play_time FROM scores "
            "WHERE map_md5 = :map_md5 AND userid = :userid AND mode = :mode AND status > 0 "
            "ORDER BY pp DESC",
            pair
        )

        best = sorted(scores, key=lambda x: (-x['pp'], x['play_time']))[0]

        if best['status'] != 2:
            if DEBUG:
                print(f"Status mismatch on score {best['id']}")

            await ctx.database.execute(
                "UPDATE scores SET status = 1 "
                "WHERE map_md5 = :map_md5 AND userid = :userid AND mode = :mode AND status > 0",
                pair
            )

            await ctx.database.execute(
                "UPDATE scores SET status = 2 WHERE id = :id",
                {"id": best['id']}
            )

    return


async def main(argv: Sequence[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if len(argv) == 0:
        argv = ["--help"]

    parser = argparse.ArgumentParser(
        description="Provides tools for recalculating the PP and status of scores, and stats of users."
    )

    parser.add_argument("-d", "--debug", action="store_true")
    parser.add_argument(
        "--scores",
        help="Recalculates the PP of all scores",
        action="store_true"
    )
    parser.add_argument(
        "--stats",
        help="Recalculates the total PP and accuracy of all users",
        action="store_true"
    )
    parser.add_argument(
        "--statuses",
        help="Re-evaluates the submission status (BEST, SUBMITTED) of scores",
        action="store_true"
    )

    parser.add_argument(
        "-m",
        "--mode",
        nargs=argparse.ONE_OR_MORE,
        required=False,
        default=["0", "1", "2", "3", "4", "5", "6", "8"],
        # would love to do things like "vn!std", but "!" will break interpretation
        choices=["0", "1", "2", "3", "4", "5", "6", "8"],
    )
    args = parser.parse_args(argv)

    global DEBUG
    DEBUG = args.debug

    db = databases.Database(app.settings.DB_DSN)
    await db.connect()

    redis = await aioredis.from_url(app.settings.REDIS_DSN)

    ctx = Context(db, redis)

    for mode in args.mode:
        mode = GameMode(int(mode))

        if args.scores:
            await recalculate_mode_scores(mode, ctx)

        if args.stats:
            await recalculate_mode_users(mode, ctx)

        if args.statuses:
            await recalculate_score_status(mode, ctx)

    await app.state.services.http_client.aclose()
    await db.disconnect()
    await redis.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
