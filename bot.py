"""Discord bot: slash commands, rank roles, member registration, role sync."""
from __future__ import annotations

import logging
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

import core
import db
from config import (
    ADMIN_IDS,
    COMMAND_GUILD_ID,
    OFFICER_ROLE_IDS,
    oauth_configured,
)

log = logging.getLogger("faction.bot")


def _embed(title: str, description: str = "", color: int = 0x5865F2) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=color)


def _fmt_delta(delta: int) -> str:
    if delta > 0:
        return f"`+{delta}` 🟢"
    if delta < 0:
        return f"`{delta}` 🔴"
    return "`0` ⚪"


def _rank_color(source: Any, key: str = "color") -> int:
    """Parse a hex colour from a row/dict field, falling back to Discord blurple."""
    try:
        raw = source[key] if not isinstance(source, str) else source
        if raw is None:
            return 0x5865F2
        return int(str(raw).lstrip("#"), 16)
    except (KeyError, IndexError, TypeError, ValueError):
        return 0x5865F2


class FactionBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.members = True  # needed for role assignment
        super().__init__(intents=intents, command_prefix="!", help_command=None)
        self.guild_id: int | None = COMMAND_GUILD_ID

    # ------------------------------------------------------------------ lifecycle
    async def setup_hook(self) -> None:
        core.set_role_syncer(self.sync_member_role)
        guild = discord.Object(id=self.guild_id) if self.guild_id else None
        if guild is not None:
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
        else:
            synced = await self.tree.sync()
        log.info("Slash-команд синхронизировано: %d", len(synced))

    async def on_ready(self) -> None:
        log.info("Бот запущен как %s (id=%s)", self.user, getattr(self.user, "id", "?"))
        if self.guild_id is None and self.guilds:
            self.guild_id = self.guilds[0].id
        if self.guild_id:
            db.set_setting("guild_id", str(self.guild_id))
            for g in self.guilds:
                if g.id == self.guild_id:
                    db.set_setting("guild_name", g.name)
        await self.refresh_roster()

    async def on_member_join(self, member: discord.Member) -> None:
        if self.guild_id and member.guild.id != self.guild_id:
            return
        db.upsert_member(
            member.id,
            username=str(member),
            display_name=member.display_name,
            avatar=str(member.display_avatar.url),
        )

    # -------------------------------------------------------------------- helpers
    def _guild(self) -> discord.Guild | None:
        if self.guild_id is None:
            return None
        return self.get_guild(self.guild_id)

    async def refresh_roster(self) -> None:
        """Cache member profiles and mirror role membership for the main guild."""
        guild = self._guild()
        if guild is None:
            return
        for m in guild.members:
            if m.bot:
                continue
            db.upsert_member(
                m.id,
                username=str(m),
                display_name=m.display_name,
                avatar=str(m.display_avatar.url),
            )

    async def sync_member_role(self, discord_id: int, role_id: int) -> str:
        """Give the member their rank role, strip the other rank roles."""
        guild = self._guild()
        if guild is None:
            return "гильдия не определена"
        try:
            member = guild.get_member(int(discord_id)) or await guild.fetch_member(int(discord_id))
        except discord.HTTPException as exc:
            return f"участник не найден в гильдии: {exc}"
        target = guild.get_role(int(role_id))
        if target is None:
            return f"роль {role_id} не найдена на сервере"
        other_ids = {int(r["role_id"]) for r in db.list_ranks() if r["role_id"]}
        other_ids.discard(int(role_id))
        removable = [r for r in member.roles if r.id in other_ids]
        try:
            if member.get_role(int(role_id)) is None:
                if removable:
                    await member.remove_roles(*removable, reason="Смена ранга фракции")
                await member.add_roles(target, reason="Смена ранга фракции")
            elif removable:
                await member.remove_roles(*removable, reason="Смена ранга фракции")
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("Не удалось выдать роль %s участнику %s: %s", role_id, discord_id, exc)
            return "нет прав на управление ролями (проверьте иерархию ролей бота)"
        return ""


def is_officer(interaction: discord.Interaction) -> bool:
    if interaction.user.id in ADMIN_IDS:
        return True
    perms = getattr(interaction.user, "guild_permissions", None)
    if perms is not None and (perms.administrator or perms.manage_guild):
        return True
    roles = getattr(interaction.user, "roles", ())
    if OFFICER_ROLE_IDS and {r.id for r in roles} & OFFICER_ROLE_IDS:
        return True
    return False


def officer_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        if is_officer(interaction):
            return True
        await interaction.response.send_message(
            "⛔ Команда доступна только офицерам фракции.", ephemeral=True
        )
        return False

    return app_commands.check(predicate)


def actor_of(interaction: discord.Interaction) -> db.Actor:
    return db.Actor(
        id=interaction.user.id,
        name=str(interaction.user),
        is_officer=is_officer(interaction),
    )


def build_bot() -> FactionBot:
    bot = FactionBot()

    # ------------------------------------------------------------------- /panel
    @bot.tree.command(name="panel", description="Ссылка на личную панель управления")
    async def panel(interaction: discord.Interaction) -> None:
        from config import BASE_URL

        db.upsert_member(
            interaction.user.id,
            username=str(interaction.user),
            display_name=interaction.user.display_name,
            avatar=str(interaction.user.display_avatar.url),
        )
        desc = (
            f"**{core.faction_name()}**\n\n"
            f"🔐 [Войти через Discord]({BASE_URL}/login)\n"
            f"После входа вам доступно личное дело с отметками и текущим рангом."
        )
        if not oauth_configured():
            desc += "\n\n⚠️ OAuth ещё не настроен: заполните `CLIENT_ID` и `CLIENT_SECRET` в `.env`."
        await interaction.response.send_message(embed=_embed("Личная панель", desc), ephemeral=True)

    # -------------------------------------------------------------------- /rank
    rank_group = app_commands.Group(name="rank", description="Управление рангами фракции")

    @rank_group.command(name="list", description="Показать лестницу рангов")
    async def rank_list(interaction: discord.Interaction) -> None:
        ranks = db.list_ranks()
        if not ranks:
            await interaction.response.send_message("Ранги ещё не настроены.", ephemeral=True)
            return
        embed = _embed(f"Ранги — {core.faction_name()}")
        for r in ranks:
            role = f"<@&{r['role_id']}>" if r["role_id"] else "*роль не привязана*"
            embed.add_field(
                name=f"{r['name']}  ·  ур. {r['level']}",
                value=f"{role}\nот {r['min_score']} очков · `{r['color']}`",
                inline=False,
            )
        await interaction.response.send_message(embed=embed)

    @rank_group.command(name="create", description="Создать ранг")
    @officer_only()
    @app_commands.describe(
        name="Название ранга",
        role="Роль Discord, которая выдаётся с этим рангом",
        level="Позиция в иерархии (больше = выше)",
        min_score="Минимальный счёт для автоматического повышения",
        color="Цвет в hex, например #f87171",
    )
    async def rank_create(
        interaction: discord.Interaction,
        name: str,
        role: discord.Role | None = None,
        level: int = 0,
        min_score: int = 0,
        color: str = "#6b7280",
    ) -> None:
        try:
            rank_id = db.create_rank(
                name=name,
                role_id=role.id if role else None,
                level=level,
                min_score=min_score,
                color=color,
            )
        except Exception as exc:  # noqa: BLE001 - surfaced to the officer
            await interaction.response.send_message(f"⚠️ Не удалось создать ранг: {exc}", ephemeral=True)
            return
        db.log_action(
            interaction.user.id, str(interaction.user), "rank.create", target=name, details=f"id={rank_id}"
        )
        await interaction.response.send_message(
            f"✅ Ранг **{name}** создан (уровень {level}, порог {min_score})."
        )

    @rank_group.command(name="edit", description="Изменить ранг")
    @officer_only()
    async def rank_edit(
        interaction: discord.Interaction,
        rank: str,
        name: str | None = None,
        role: discord.Role | None = None,
        level: int | None = None,
        min_score: int | None = None,
        color: str | None = None,
    ) -> None:
        target = _find_rank(rank)
        if target is None:
            await interaction.response.send_message("Ранг не найден.", ephemeral=True)
            return
        fields: dict[str, Any] = {}
        if name:
            fields["name"] = name
        if role is not None:
            fields["role_id"] = role.id
        if level is not None:
            fields["level"] = level
        if min_score is not None:
            fields["min_score"] = min_score
        if color:
            fields["color"] = color
        if not fields:
            await interaction.response.send_message("Нечего менять.", ephemeral=True)
            return
        db.update_rank(int(target["id"]), **fields)
        db.log_action(
            interaction.user.id,
            str(interaction.user),
            "rank.edit",
            target=str(target["name"]),
            details=", ".join(f"{k}={v}" for k, v in fields.items()),
        )
        await interaction.response.send_message(f"✅ Ранг **{target['name']}** обновлён.")

    @rank_group.command(name="delete", description="Удалить ранг")
    @officer_only()
    async def rank_delete(interaction: discord.Interaction, rank: str) -> None:
        target = _find_rank(rank)
        if target is None:
            await interaction.response.send_message("Ранг не найден.", ephemeral=True)
            return
        db.delete_rank(int(target["id"]))
        db.log_action(
            interaction.user.id, str(interaction.user), "rank.delete", target=str(target["name"])
        )
        await interaction.response.send_message(f"🗑️ Ранг **{target['name']}** удалён.")

    @rank_group.command(name="assign", description="Назначить ранг участнику")
    @officer_only()
    async def rank_assign(interaction: discord.Interaction, member: discord.Member, rank: str) -> None:
        target = _find_rank(rank)
        if target is None:
            await interaction.response.send_message("Ранг не найден.", ephemeral=True)
            return
        db.upsert_member(
            member.id,
            username=str(member),
            display_name=member.display_name,
            avatar=str(member.display_avatar.url),
        )
        await interaction.response.defer(ephemeral=True)
        err = await core.set_member_rank(member.id, int(target["id"]), actor_of(interaction))
        msg = f"✅ {member.mention} → ранг **{target['name']}**."
        if err:
            msg += f"\n⚠️ Роль на сервере: {err}"
        await interaction.followup.send(msg, ephemeral=True)

    bot.tree.add_command(rank_group)

    # -------------------------------------------------------------------- /mark
    mark_group = app_commands.Group(name="mark", description="Отметки в личном деле")

    @mark_group.command(name="give", description="Выдать отметку участнику")
    @officer_only()
    @app_commands.describe(
        member="Участник",
        title="Название отметки",
        delta="Влияние на счёт: +5, -10 или 0",
        comment="Комментарий для личного дела",
    )
    async def mark_give(
        interaction: discord.Interaction,
        member: discord.Member,
        title: str,
        delta: int = 0,
        comment: str = "",
    ) -> None:
        db.upsert_member(
            member.id,
            username=str(member),
            display_name=member.display_name,
            avatar=str(member.display_avatar.url),
        )
        await interaction.response.defer()
        result = await core.issue_mark(member.id, title, comment, delta, actor_of(interaction))
        embed = _embed(
            f"Отметка: {title}",
            f"{member.mention} · {_fmt_delta(delta)}\n"
            f"Счёт: **{result.score}**"
            + (f"\nКомментарий: {comment}" if comment else ""),
            color=_rank_color(result.new_rank) if result.new_rank else 0x5865F2,
        )
        if result.rank_changed and result.new_rank:
            arrow = "🔼" if result.promoted else "🔽"
            embed.add_field(
                name=f"{arrow} Изменение ранга",
                value=f"{result.old_rank['name'] if result.old_rank else '—'} → **{result.new_rank['name']}**",
                inline=False,
            )
        if result.role_error:
            embed.add_field(name="⚠️ Роль", value=result.role_error, inline=False)
        await interaction.followup.send(embed=embed)

    @mark_group.command(name="revoke", description="Отозвать отметку по номеру")
    @officer_only()
    async def mark_revoke(interaction: discord.Interaction, mark_id: int) -> None:
        await interaction.response.defer(ephemeral=True)
        ok, err = await core.revoke_mark(mark_id, actor_of(interaction))
        if not ok:
            await interaction.followup.send(f"⚠️ {err}", ephemeral=True)
            return
        await interaction.followup.send(f"✅ Отметка #{mark_id} отозвана.", ephemeral=True)

    bot.tree.add_command(mark_group)

    # -------------------------------------------------------------------- /file
    @bot.tree.command(name="file", description="Личное дело участника")
    async def file_cmd(interaction: discord.Interaction, member: discord.Member | None = None) -> None:
        if member is None:
            if not isinstance(interaction.user, discord.Member):
                await interaction.response.send_message(
                    "Укажите участника: команда работает с участником сервера.", ephemeral=True
                )
                return
            target: Any = interaction.user
        else:
            target = member
        db.upsert_member(
            target.id,
            username=str(target),
            display_name=target.display_name,
            avatar=str(target.display_avatar.url),
        )
        record = db.get_member_full(target.id)
        if record is None:
            await interaction.response.send_message("Личное дело не найдено.", ephemeral=True)
            return
        stats = db.mark_stats(target.id)
        progress = core.ladder_progress(int(record["score"]), db.list_ranks())
        embed_color = _rank_color(record, "rank_color")
        embed = _embed(
            f"Личное дело — {record['display_name']}",
            f"Ранг: **{record['rank_name'] or 'без ранга'}**\n"
            f"Счёт: **{record['score']}** · 🟢 {stats['positive']} · 🔴 {stats['negative']} · ⚪ {stats['neutral']}",
            color=embed_color,
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        if progress["next"]:
            embed.add_field(
                name="До следующего ранга",
                value=f"**{progress['next']['name']}** — осталось {progress['remaining']} очков ({progress['percent']}%)",
                inline=False,
            )
        else:
            embed.add_field(name="До следующего ранга", value="Максимальный ранг достигнут", inline=False)
        if record["callsign"]:
            embed.add_field(name="Позывной", value=record["callsign"], inline=True)
        if record["note"]:
            embed.add_field(name="Примечание", value=record["note"][:1024], inline=False)
        marks = db.list_marks(target.id)[:8]
        if marks:
            lines = []
            for m in marks:
                stamp = f"<t:{int(m['created_at'])}:d>"
                lines.append(f"`#{m['id']}` {_fmt_delta(int(m['delta']))} **{m['title']}** — {stamp}")
            embed.add_field(name="Последние отметки", value="\n".join(lines)[:1024], inline=False)
        else:
            embed.add_field(name="Последние отметки", value="*Отметок нет*", inline=False)
        await interaction.response.send_message(embed=embed)

    # ------------------------------------------------------------------ /roster
    @bot.tree.command(name="roster", description="Состав фракции")
    async def roster(interaction: discord.Interaction) -> None:
        members = db.list_members()
        if not members:
            await interaction.response.send_message("Состав пуст.", ephemeral=True)
            return
        lines = []
        for m in members[:40]:
            rank = m["rank_name"] or "—"
            lines.append(f"**{m['display_name']}** · {rank} · {m['score']} очк.")
        embed = _embed(
            f"Состав — {core.faction_name()}",
            "\n".join(lines) + ("\n…" if len(members) > 40 else ""),
        )
        await interaction.response.send_message(embed=embed)

    # -------------------------------------------------------------------- /sync
    @bot.tree.command(name="sync", description="Обновить состав и роли из Discord")
    @officer_only()
    async def sync(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        await bot.refresh_roster()
        ranks = {int(r["id"]): r for r in db.list_ranks()}
        fixed = 0
        for row in db.list_members():
            rank = ranks.get(int(row["rank_id"])) if row["rank_id"] else None
            if rank and rank["role_id"]:
                err = await bot.sync_member_role(int(row["discord_id"]), int(rank["role_id"]))
                if not err:
                    fixed += 1
        await interaction.followup.send(f"🔄 Состав обновлён, роли синхронизированы у {fixed} участников.", ephemeral=True)

    # ------------------------------------------------------------------ /setup
    @bot.tree.command(name="setup", description="Привязать сервер и показать статус системы")
    @officer_only()
    async def setup_cmd(interaction: discord.Interaction) -> None:
        if interaction.guild is not None:
            bot.guild_id = interaction.guild.id
            db.set_setting("guild_id", str(interaction.guild.id))
            db.set_setting("guild_name", interaction.guild.name)
        ranks = db.list_ranks()
        embed = _embed(
            "Состояние системы",
            f"Сервер: **{db.get_setting('guild_name', '—')}** (`{bot.guild_id}`)\n"
            f"Рангов: **{len(ranks)}** · Участников: **{len(db.list_members())}**\n"
            f"Веб-панель: {'✅ настроена' if oauth_configured() else '⚠️ нет CLIENT_ID/SECRET'}\n"
            f"Авторанг по очкам: {'вкл' if core.auto_rank_enabled() else 'выкл'}",
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    return bot


def _find_rank(query: str) -> Any:
    query_norm = query.strip().lower()
    ranks = db.list_ranks()
    for r in ranks:
        if str(r["name"]).lower() == query_norm:
            return r
    if query.strip().isdigit():
        return db.get_rank(int(query.strip()))
    for r in ranks:
        if query_norm in str(r["name"]).lower():
            return r
    return None


async def role_sync_job(bot: FactionBot, discord_id: int, role_id: int) -> None:
    await bot.sync_member_role(discord_id, role_id)
