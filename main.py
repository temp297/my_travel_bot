import asyncio
import logging
import os
import aiosqlite
import random
from datetime import datetime
from aiohttp import web
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import ReplyKeyboardBuilder, InlineKeyboardBuilder
from aiogram_calendar import SimpleCalendar, SimpleCalendarCallback
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiogram.exceptions import TelegramBadRequest


    scheduler.start()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
