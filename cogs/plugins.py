import asyncio
import io
import json
import os
import shutil
import sys
import typing
import zipfile
from importlib import invalidate_caches
from difflib import get_close_matches
from pathlib import Path, PurePath
from re import match
from site import USER_SITE
from subprocess import PIPE

import discord
from discord.ext import commands

from pkg_resources import parse_version

from core import checks
from core.models import PermissionLevel, getLogger
from core.paginator import EmbedPaginatorSession
from core.utils import truncate, trigger_typing

logger = getLogger(__name__)


class InvalidPluginError(commands.BadArgument):
    pass


class Plugin:
    def __init__(self, user, repo, name, branch=None):
        self.user = user
        self.repo = repo
        self.name = name
        self.branch = branch if branch is not None else "master"
        self.url = f"https://github.com/{user}/{repo}/archive/{self.branch}.zip"
        self.link = f"https://github.com/{user}/{repo}/tree/{self.branch}/{name}"

    @property
    def path(self):
        return PurePath("plugins") / self.user / self.repo / f"{self.name}-{self.branch}"

    @property
    def abs_path(self):
        return Path(__file__).absolute().parent.parent / self.path

    @property
    def cache_path(self):
        return (
            Path(__file__).absolute().parent.parent
            / "temp"
            / "plugins-cache"
            / f"{self.user}-{self.repo}-{self.branch}.zip"
        )

    @property
    def ext_string(self):
        return f"plugins.{self.user}.{self.repo}.{self.name}-{self.branch}.{self.name}"

    def __str__(self):
        return f"{self.user}/{self.repo}/{self.name}@{self.branch}"

    def __lt__(self, other):
        return self.name.lower() < other.name.lower()

    @classmethod
    def from_string(cls, s, strict=False):
        if not strict:
            m = match(r"^(.+?)/(.+?)/(.+?)(?:@(.+?))?$", s)
        else:
            m = match(r"^(.+?)/(.+?)/(.+?)@(.+?)$", s)
        if m is not None:
            return Plugin(*m.groups())
        raise InvalidPluginError("Cannot decipher %s.", s)  # pylint: disable=raising-format-tuple

    def __hash__(self):
        return hash((self.user, self.repo, self.name, self.branch))

    def __repr__(self):
        return f"<Plugins: {self.__str__()}>"

    def __eq__(self, other):
        return isinstance(other, Plugin) and self.__str__() == other.__str__()


class Plugins(commands.Cog):
    """
    Plugins expand Modmail functionality by allowing third-party addons.

    These addons could have a range of features from moderation to simply
    making your life as a moderator easier!
    Learn how to create a plugin yourself here:
    https://github.com/kyb3r/modmail/wiki/Plugins
    """

    def __init__(self, bot):
        self.bot = bot
        self.registry = {}
        self.loaded_plugins = set()
        self._ready_event = asyncio.Event()

        self.bot.loop.create_task(self.populate_registry())

        if self.bot.config.get("enable_plugins"):
            self.bot.loop.create_task(self.initial_load_plugins())
        else:
            logger.info("Plugins not loaded since ENABLE_PLUGINS=false.")

    async def populate_registry(self):
        url = "https://raw.githubusercontent.com/kyb3r/modmail/master/plugins/registry.json"
        async with self.bot.session.get(url) as resp:
            self.registry = json.loads(await resp.text())

    async def initial_load_plugins(self):
        await self.bot.wait_for_connected()

        for plugin_name in list(self.bot.config["plugins"]):
            try:
                plugin = Plugin.from_string(plugin_name, strict=True)
            except InvalidPluginError:
                self.bot.config["plugins"].remove(plugin_name)
                try:
                    # For backwards compat
                    plugin = Plugin.from_string(plugin_name)
                except InvalidPluginError:
                    logger.error("Failed to parse plugin name: %s.", plugin_name, exc_info=True)
                    continue

                logger.info("Migrated legacy plugin name: %s, now %s.", plugin_name, str(plugin))
                self.bot.config["plugins"].append(str(plugin))

            try:
                await self.download_plugin(plugin)
                await self.load_plugin(plugin)
            except Exception:
                self.bot.config["plugins"].remove(plugin_name)
                logger.error(
                    "Error when loading plugin %s. Plugin removed from config.",
                    plugin,
                    exc_info=True,
                )
                continue

        logger.debug("Finished loading all plugins.")

        self.bot.dispatch("plugins_ready")

        self._ready_event.set()
        await self.bot.config.update()

    async def download_plugin(self, plugin, force=False):
        if plugin.abs_path.exists() and not force:
            return

        plugin.abs_path.mkdir(parents=True, exist_ok=True)

        if plugin.cache_path.exists() and not force:
            plugin_io = plugin.cache_path.open("rb")
            logger.debug("Loading cached %s.", plugin.cache_path)
        else:
            headers = {}
            github_token = self.bot.config["github_token"]
            if github_token is not None:
                headers["Authorization"] = f"token {github_token}"

            async with self.bot.session.get(plugin.url, headers=headers) as resp:
                logger.debug("Downloading %s.", plugin.url)
                raw = await resp.read()

                try:
                    raw = await resp.text()
                except UnicodeDecodeError:
                    pass
                else:
                    if raw == "Not Found":
                        raise InvalidPluginError("Plugin not found")
                    else:
                        raise InvalidPluginError("Invalid download recieved, non-bytes object")

                plugin_io = io.BytesIO(raw)
                if not plugin.cache_path.parent.exists():
                    plugin.cache_path.parent.mkdir(parents=True)

                with plugin.cache_path.open("wb") as f:
                    f.write(raw)

        with zipfile.ZipFile(plugin_io) as zipf:
            for info in zipf.infolist():
                path = PurePath(info.filename)
                if len(path.parts) >= 3 and path.parts[1] == plugin.name:
                    plugin_path = plugin.abs_path / Path(*path.parts[2:])
                    if info.is_dir():
                        plugin_path.mkdir(parents=True, exist_ok=True)
                    else:
                        plugin_path.parent.mkdir(parents=True, exist_ok=True)
                        with zipf.open(info) as src, plugin_path.open("wb") as dst:
                            shutil.copyfileobj(src, dst)

        plugin_io.close()

    async def load_plugin(self, plugin):
        if not (plugin.abs_path / f"{plugin.name}.py").exists():
            raise InvalidPluginError(f"{plugin.name}.py not found.")

        req_txt = plugin.abs_path / "requirements.txt"

        if req_txt.exists():
            # Install PIP requirements

            venv = hasattr(sys, "real_prefix") or hasattr(sys, "base_prefix")  # in a virtual env
            user_install = " --user" if not venv else ""
            proc = await asyncio.create_subprocess_shell(
                f'"{sys.executable}" -m pip install --upgrade{user_install} -r {req_txt} -q -q',
                stderr=PIPE,
                stdout=PIPE,
            )

            logger.debug("Downloading requirements for %s.", plugin.ext_string)

            stdout, stderr = await proc.communicate()

            if stdout:
                logger.debug("[stdout]\n%s.", stdout.decode())

            if stderr:
                logger.debug("[stderr]\n%s.", stderr.decode())
                logger.error(
                    "Failed to download requirements for %s.", plugin.ext_string, exc_info=True
                )
                raise InvalidPluginError(
                    f"Unable to download requirements: ```\n{stderr.decode()}\n```"
                )

            if os.path.exists(USER_SITE):
                sys.path.insert(0, USER_SITE)

        try:
            self.bot.load_extension(plugin.ext_string)
            logger.info("Loaded plugin: %s", plugin.ext_string.split(".")[-1])
            self.loaded_plugins.add(plugin)

        except commands.ExtensionError as exc:
            logger.error("Plugin load failure: %s", plugin.ext_string, exc_info=True)
            raise InvalidPluginError("Cannot load extension, plugin invalid.") from exc

    async def parse_user_input(self, ctx, plugin_name, check_version=False):

        if not self.bot.config["enable_plugins"]:
            embed = discord.Embed(
                description="Plugins are disabled, enable them by setting `ENABLE_PLUGINS=true`",
                color=self.bot.main_color,
            )
            await ctx.send(embed=em)
            return

        if not self._ready_event.is_set():
            embed = discord.Embed(
                description="Plugins are still loading, please try again later.",
                color=self.bot.main_color,
            )
            await ctx.send(embed=embed)
            return

        if plugin_name in self.registry:
            details = self.registry[plugin_name]
            user, repo = details["repository"].split("/", maxsplit=1)
            branch = details.get("branch")

            if check_version:
                required_version = details.get("bot_version", False)

                if required_version and self.bot.version < parse_version(required_version):
                    embed = discord.Embed(
                        description="Your bot's version is too low. "
                        f"This plugin requires version `{required_version}`.",
                        color=self.bot.error_color,
                    )
                    await ctx.send(embed=embed)
                    return

            plugin = Plugin(user, repo, plugin_name, branch)

        else:
            try:
                plugin = Plugin.from_string(plugin_name)
            except InvalidPluginError:
                embed = discord.Embed(
                    description="Invalid plugin name, double check the plugin name "
                    "or use one of the following formats: "
                    "username/repo/plugin, username/repo/plugin@branch.",
                    color=self.bot.error_color,
                )
                await ctx.send(embed=embed)
                return
        return plugin

def setup(bot):
    bot.add_cog(Plugins(bot))
