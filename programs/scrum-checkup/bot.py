"""
This module creates and assembles the discord bot.
Components include:
- Configuration
- Session manager
- Message processor
- Engine manager

The bot is started here.
"""

import asyncio
import logging
import threading
from typing import Optional

import discord
from discord.ext import commands
from llmgine.bootstrap import ApplicationBootstrap
from llmgine.bus.bus import MessageBus
from llmgine.messages import CommandResult

from program_types import CheckUpEvent
from scrum_engine import request_end_conversation
from config import DiscordBotConfig
from scrum_engine import (
    ScrumMasterEngine,
    ScrumMasterCommand,
    ScrumMasterEngineStatusEvent,
    ScrumMasterConfirmEndConversationCommand,
    ScrumMasterEngineToolResultEvent,
)

from datetime import datetime
from components import YesNoView
from program_types import SessionContext, DiscordChannelID

# Configure logging
logging.basicConfig(level=logging.INFO)


class ScrumMasterBot:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                # Double-check locking pattern
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        # Only initialize once
        if hasattr(self, "_initialized"):
            return

        # Load configuration
        self.config = DiscordBotConfig.load_from_env()

        # Initialize Discord bot
        intents: discord.Intents = discord.Intents.default()
        intents.message_content = True
        intents.messages = True
        self.bot: commands.Bot = commands.Bot(command_prefix="!", intents=intents)

        # Initialize managers
        self.channel_register: dict[
            str, tuple[str, ScrumMasterEngine, SessionContext]
        ] = {}

        # Set up event handlers
        self.bot.event(self.on_message)
        self.bot.event(self.on_ready)

        # Mark as initialized
        self._initialized = True

    @classmethod
    def get_instance(cls) -> "ScrumMasterBot":
        """Get the singleton instance. Preferred method for accessing the singleton."""
        if cls._instance is None:
            cls()
        return cls._instance  # type: ignore

    async def create_session(
        self,
        session_id: str,
        channel_id: str,
        engine: ScrumMasterEngine,
        user_discord_id: str,
    ) -> None:
        self.channel_register[str(channel_id)] = (
            session_id,
            engine,
            SessionContext({}),
        )
        await engine.tool_manager.register_tool(request_end_conversation)
        MessageBus().register_command_handler(
            ScrumMasterConfirmEndConversationCommand,
            self.handle_end_conversation,
            session_id=session_id,
        )
        init_message = await engine.handle_command(
            ScrumMasterCommand(prompt="Start the process", session_id=session_id)
        )
        channel = self.bot.get_channel(int(channel_id))
        if channel is None:
            raise ValueError(f"Channel {int(channel_id)} not found")
        await channel.send(
            f"# Scrum Checkup <@{user_discord_id}>\n\n"
            + (init_message.result if init_message.result else "")
        )

    async def end_session(self, channel_id: DiscordChannelID) -> None:
        print(f"Ending session for channel {channel_id}")
        conversation = await self.channel_register[str(channel_id)][
            1
        ].extract_conversation()[1:]  # remove the system prompt
        await MessageBus().publish(
            CheckUpFinishedEvent(
                discord_channel_id=channel_id, conversation=conversation
            )
        )
        self.channel_register.pop(str(channel_id))

    async def on_ready(self) -> None:
        """Called when the bot is ready and connected to Discord."""
        print(f"Bot is ready! Logged in as {self.bot.user}")
        # Now the bot can access channels

        print(f"\n\nScrumMasterBot instance on_ready: {self.get_instance()}\n\n")
        await MessageBus().publish(CheckUpEvent(user_discord_id="774065995508744232"))

    async def on_message(self, message: discord.Message) -> None:
        """Handle incoming messages."""
        if message.author == self.bot.user:
            return

        if message.mention_everyone:
            return

        if str(message.channel.id) in self.channel_register.keys():
            async with self.show_loading_message(
                DiscordChannelID(str(message.channel.id)), message
            ):
                response = await self.channel_register[str(message.channel.id)][
                    1
                ].handle_command(ScrumMasterCommand(prompt=message.content))
            await message.reply(response.result)
        else:
            print("Message received in unregistered channel")

    def show_loading_message(
        self, channel_id: DiscordChannelID, original_message: discord.Message
    ) -> "LoadingMessageContext":
        return LoadingMessageContext(self.bot, channel_id, original_message)

    async def start(self):
        """Start the bot and all necessary services."""
        # Bootstrap the application
        bootstrap = ApplicationBootstrap(self.config)
        await bootstrap.bootstrap()

        # Start the message bus
        bus: MessageBus = MessageBus()
        await bus.start()

        try:
            # Run the bot
            await self.bot.start(self.config.bot_key)
        finally:
            # Ensure the bus is stopped when the application ends
            await bus.stop()

    async def request_end_conversation(
        self,
        channel_id: DiscordChannelID,
    ) -> bool:
        """Request input from a user for a specific session"""

        view = YesNoView(timeout=60)
        channel = self.bot.get_channel(int(channel_id))
        prompt_msg = await channel.send(
            content="⚠️ Stella would like to end the conversation. Do you agree?",
            view=view,
        )
        await view.wait()

        # Process the result
        if view.value is None:
            result = False
            await prompt_msg.edit(content="⏱️ Request timed out", view=None)
        else:
            result = view.value
            resp_text = (
                "✅ End conversation request accepted"
                if view.value
                else "❌ End conversation request declined"
            )
            await prompt_msg.edit(content=f"{resp_text}", view=None)

        return result if result is not None else False

    async def handle_end_conversation(
        self, command: ScrumMasterConfirmEndConversationCommand
    ) -> CommandResult:
        result = await self.request_end_conversation(command.channel_id)
        if result:
            await self.end_session(command.channel_id)
        return CommandResult(
            success=True,
            result=result,
        )


class LoadingMessageContext:
    def __init__(
        self,
        bot: commands.Bot,
        channel_id: DiscordChannelID,
        original_message: discord.Message,
    ) -> None:
        self.bot = bot
        self.channel_id = channel_id
        self.original_message = original_message
        self.loading_message: discord.Message | None = None

    async def __aenter__(self) -> "LoadingMessageContext":
        self.loading_message = await self.original_message.reply(
            "🔄 Generating Response..."
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        if self.loading_message:
            try:
                await self.loading_message.delete()
            except discord.NotFound:
                # Message was already deleted
                pass
            except discord.Forbidden:
                # Bot doesn't have permission to delete
                pass
