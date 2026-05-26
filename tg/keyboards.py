from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from . import callbacks as cb


def option_type_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Call", callback_data=cb.CALL),
        InlineKeyboardButton("Put",  callback_data=cb.PUT),
    ]])


def confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Confirm", callback_data=cb.CONFIRM),
        InlineKeyboardButton("Cancel",  callback_data=cb.CANCEL),
    ]])
