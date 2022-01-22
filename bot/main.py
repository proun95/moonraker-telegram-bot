import argparse
import configparser
import faulthandler
import hashlib
import itertools
import logging
from logging.handlers import RotatingFileHandler
import os
import sys
from pathlib import Path
from zipfile import ZipFile

import ujson
from numpy import random
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatAction, ReplyKeyboardMarkup, Message, MessageEntity
from telegram.constants import PARSEMODE_MARKDOWN_V2
from telegram.error import BadRequest
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext, CallbackQueryHandler
import websocket
from telegram.utils.helpers import escape_markdown

from configuration import ConfigWrapper
from camera import Camera
from klippy import Klippy
from notifications import Notifier
from power_device import PowerDevice
from timelapse import Timelapse

try:
    import thread
except ImportError:
    import _thread as thread

from io import BytesIO
import emoji
from apscheduler.schedulers.background import BackgroundScheduler

logging.basicConfig(
    handlers=[
        logging.StreamHandler(sys.stdout)
    ],
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

logger = logging.getLogger(__name__)


def handle_exception(exc_type, exc_value, exc_traceback):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return

    logger.error("Uncaught exception", exc_info=(exc_type, exc_value, exc_traceback))


sys.excepthook = handle_exception

# some global params
bot_updater: Updater
scheduler = BackgroundScheduler({
    'apscheduler.executors.default': {
        'class': 'apscheduler.executors.pool:ThreadPoolExecutor',
        'max_workers': '10'
    },
    'apscheduler.job_defaults.coalesce': 'false',
    'apscheduler.job_defaults.max_instances': '1',
}, daemon=True)

configWrap: ConfigWrapper = None
myId = random.randint(300000)
cameraWrap: Camera
timelapse: Timelapse
notifier: Notifier
ws: websocket.WebSocketApp = None
klippy: Klippy
light_power_device: PowerDevice
psu_power_device: PowerDevice


def echo_unknown(update: Update, _: CallbackContext) -> None:
    update.message.reply_text(f"unknown command: {update.message.text}", quote=True)


def unknown_chat(update: Update, _: CallbackContext) -> None:
    message = f"Unauthorized access detected with chat_id: {update.effective_chat.id}.\n"
    update.message.reply_text(f"{message}This incident will be reported.", entities=[MessageEntity(type='spoiler', offset=len(message), length=31)], quote=True)
    logger.error(f"Unauthorized access detected from `{update.effective_chat.username}` with chat_id `{update.effective_chat.id}`. Message: {update.effective_message.to_json()}")


def status(update: Update, _: CallbackContext) -> None:
    message_to_reply = update.message if update.message else update.effective_message
    if klippy.printing:
        notifier.update_status()
        import time
        time.sleep(configWrap.camera.light_timeout + 3)
        message_to_reply.delete()
    else:
        mess = escape_markdown(klippy.get_status(), version=2)
        if cameraWrap.enabled:
            with cameraWrap.take_photo() as bio:
                message_to_reply.bot.send_chat_action(chat_id=configWrap.bot.chat_id, action=ChatAction.UPLOAD_PHOTO)
                message_to_reply.reply_photo(photo=bio, caption=mess, parse_mode=PARSEMODE_MARKDOWN_V2, disable_notification=notifier.silent_commands)
                bio.close()
        else:
            message_to_reply.bot.send_chat_action(chat_id=configWrap.bot.chat_id, action=ChatAction.TYPING)
            message_to_reply.reply_text(mess, parse_mode=PARSEMODE_MARKDOWN_V2, disable_notification=notifier.silent_commands, quote=True)


def check_unfinished_lapses():
    files = cameraWrap.detect_unfinished_lapses()
    if not files:
        return
    bot_updater.bot.send_chat_action(chat_id=configWrap.bot.chat_id, action=ChatAction.TYPING)
    files_keys = list(map(list, zip(map(lambda el: InlineKeyboardButton(text=el, callback_data=f'lapse:{hashlib.md5(el.encode()).hexdigest()}'), files))))
    files_keys.append([InlineKeyboardButton(emoji.emojize(':no_entry_sign: ', use_aliases=True), callback_data='do_nothing')])
    reply_markup = InlineKeyboardMarkup(files_keys)
    bot_updater.bot.send_message(configWrap.bot.chat_id, text='Unfinished timelapses found\nBuild unfinished timelapse?', reply_markup=reply_markup, disable_notification=notifier.silent_status)


def get_video(update: Update, _: CallbackContext) -> None:
    message_to_reply = update.message if update.message else update.effective_message
    if not cameraWrap.enabled:
        message_to_reply.reply_text("camera is disabled", quote=True)
    else:
        info_reply: Message = message_to_reply.reply_text(text=f"Starting video recording", disable_notification=notifier.silent_commands, quote=True)
        message_to_reply.bot.send_chat_action(chat_id=configWrap.bot.chat_id, action=ChatAction.RECORD_VIDEO)
        with cameraWrap.take_video_generator() as (video_bio, thumb_bio, width, height):
            info_reply.edit_text(text="Uploading video")
            if video_bio.getbuffer().nbytes > 52428800:
                info_reply.edit_text(text='Telegram has a 50mb restriction...')
            else:
                message_to_reply.reply_video(video=video_bio, thumb=thumb_bio, width=width, height=height, caption='', timeout=120, disable_notification=notifier.silent_commands, quote=True)
                message_to_reply.bot.delete_message(chat_id=configWrap.bot.chat_id, message_id=info_reply.message_id)

            video_bio.close()
            thumb_bio.close()


def manage_printing(command: str) -> None:
    ws.send(ujson.dumps({"jsonrpc": "2.0", "method": f"printer.print.{command}", "id": myId}))


def emergency_stop_printer():
    ws.send(ujson.dumps({"jsonrpc": "2.0", "method": f"printer.emergency_stop", "id": myId}))


def shutdown_pi_host():
    ws.send(ujson.dumps({"jsonrpc": "2.0", "method": f"machine.shutdown", "id": myId}))


def confirm_keyboard(callback_mess: str) -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton(emoji.emojize(':white_check_mark: ', use_aliases=True), callback_data=callback_mess),
            InlineKeyboardButton(emoji.emojize(':no_entry_sign: ', use_aliases=True), callback_data='do_nothing'),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


def pause_printing(update: Update, __: CallbackContext) -> None:
    update.message.bot.send_chat_action(chat_id=configWrap.bot.chat_id, action=ChatAction.TYPING)
    update.message.reply_text('Pause printing?', reply_markup=confirm_keyboard('pause_printing'), disable_notification=notifier.silent_commands, quote=True)


def resume_printing(update: Update, __: CallbackContext) -> None:
    update.message.bot.send_chat_action(chat_id=configWrap.bot.chat_id, action=ChatAction.TYPING)
    update.message.reply_text('Resume printing?', reply_markup=confirm_keyboard('resume_printing'), disable_notification=notifier.silent_commands, quote=True)


def cancel_printing(update: Update, __: CallbackContext) -> None:
    update.message.bot.send_chat_action(chat_id=configWrap.bot.chat_id, action=ChatAction.TYPING)
    update.message.reply_text('Cancel printing?', reply_markup=confirm_keyboard('cancel_printing'), disable_notification=notifier.silent_commands, quote=True)


def emergency_stop(update: Update, _: CallbackContext) -> None:
    update.message.bot.send_chat_action(chat_id=configWrap.bot.chat_id, action=ChatAction.TYPING)
    update.message.reply_text('Execute emergency stop?', reply_markup=confirm_keyboard('emergency_stop'), disable_notification=notifier.silent_commands, quote=True)


def shutdown_host(update: Update, _: CallbackContext) -> None:
    update.message.bot.send_chat_action(chat_id=configWrap.bot.chat_id, action=ChatAction.TYPING)
    update.message.reply_text('Shutdown host?', reply_markup=confirm_keyboard('shutdown_host'), disable_notification=notifier.silent_commands, quote=True)


def bot_restart(update: Update, _: CallbackContext) -> None:
    update.message.bot.send_chat_action(chat_id=configWrap.bot.chat_id, action=ChatAction.TYPING)
    update.message.reply_text('Restart bot?', reply_markup=confirm_keyboard('bot_restart'), disable_notification=notifier.silent_commands, quote=True)


def restart_bot() -> None:
    if ws:
        ws.close()
    os._exit(1)


def power(update: Update, _: CallbackContext) -> None:
    message_to_reply = update.message if update.message else update.effective_message
    message_to_reply.bot.send_chat_action(chat_id=configWrap.bot.chat_id, action=ChatAction.TYPING)
    if psu_power_device:
        if psu_power_device.device_state:
            message_to_reply.reply_text('Power Off printer?', reply_markup=confirm_keyboard('power_off_printer'), disable_notification=notifier.silent_commands, quote=True)
        else:
            message_to_reply.reply_text('Power On printer?', reply_markup=confirm_keyboard('power_on_printer'), disable_notification=notifier.silent_commands, quote=True)
    else:
        message_to_reply.reply_text("No power device in config!", disable_notification=notifier.silent_commands, quote=True)


def light_toggle(update: Update, _: CallbackContext) -> None:
    message_to_reply = update.message if update.message else update.effective_message
    if light_power_device:
        light_power_device.toggle_device()
    else:
        message_to_reply.reply_text("No light device in config!", disable_notification=notifier.silent_commands, quote=True)


def button_handler(update: Update, context: CallbackContext) -> None:
    context.bot.send_chat_action(chat_id=configWrap.bot.chat_id, action=ChatAction.TYPING)
    query = update.callback_query
    query.answer()
    # Todo: maybe regex check?
    if query.data == 'do_nothing':
        if update.effective_message.reply_to_message:
            context.bot.delete_message(update.effective_message.chat_id, update.effective_message.reply_to_message.message_id)
        query.delete_message()
    elif query.data == 'emergency_stop':
        emergency_stop_printer()
        query.delete_message()
    elif query.data == 'shutdown_host':
        update.effective_message.reply_text("Shutting down bot", quote=True)
        shutdown_pi_host()
    elif query.data == 'bot_restart':
        update.effective_message.reply_text("Restarting bot", quote=True)
        restart_bot()
    elif query.data == 'cancel_printing':
        manage_printing('cancel')
        query.delete_message()
    elif query.data == 'pause_printing':
        manage_printing('pause')
        query.delete_message()
    elif query.data == 'resume_printing':
        manage_printing('resume')
        query.delete_message()
    elif query.data == 'power_off_printer':
        psu_power_device.switch_device(False)
        query.delete_message()
    elif query.data == 'power_on_printer':
        psu_power_device.switch_device(True)
        query.delete_message()
    elif 'macro:' in query.data:
        command = query.data.replace('macro:', '')
        update.effective_message.reply_text(f"Running macro: {command}", disable_notification=notifier.silent_commands, quote=True)
        query.delete_message()
        klippy.execute_command(command)
    elif 'macroc:' in query.data:
        command = query.data.replace('macroc:', '')
        query.edit_message_text(text=f"Execute marco {command}?", reply_markup=confirm_keyboard(f'macro:{command}'))
    elif '.gcode' in query.data and ':' not in query.data:
        keyboard_keys = dict((x['callback_data'], x['text']) for x in itertools.chain.from_iterable(query.message.reply_markup.to_dict()['inline_keyboard']))
        filename = keyboard_keys[query.data]
        keyboard = [
            [
                InlineKeyboardButton(emoji.emojize(':robot: print file', use_aliases=True), callback_data=f'print_file:{query.data}'),
                InlineKeyboardButton(emoji.emojize(':cross_mark: cancel', use_aliases=True), callback_data='cancel_file'),
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        start_pre_mess = 'Start printing file:'
        message, bio = klippy.get_file_info_by_name(filename, f"{start_pre_mess}{filename}?")
        if bio is not None:
            update.effective_message.reply_to_message.reply_photo(photo=bio, caption=message, reply_markup=reply_markup, disable_notification=notifier.silent_commands, quote=True,
                                                                  caption_entities=[MessageEntity(type='bold', offset=len(start_pre_mess), length=len(filename))])
            bio.close()
            context.bot.delete_message(update.effective_message.chat_id, update.effective_message.message_id)
        else:
            query.edit_message_text(text=message, reply_markup=reply_markup, entities=[MessageEntity(type='bold', offset=len(start_pre_mess), length=len(filename))])
    elif 'print_file' in query.data:
        if query.message.caption:
            filename = query.message.parse_caption_entity(query.message.caption_entities[0]).strip()
        else:
            filename = query.message.parse_entity(query.message.entities[0]).strip()
        if klippy.start_printing_file(filename):
            query.delete_message()
        else:
            if query.message.text:
                query.edit_message_text(text=f"Failed start printing file {filename}")
            elif query.message.caption:
                query.message.edit_caption(caption=f"Failed start printing file {filename}")
    elif 'lapse:' in query.data:
        lapse_name = next(filter(lambda el: el[0].callback_data == query.data, query.message.reply_markup.inline_keyboard))[0].text
        info_mess: Message = query.bot.send_message(chat_id=configWrap.bot.chat_id, text=f"Starting time-lapse assembly for {lapse_name}", disable_notification=notifier.silent_commands)
        query.bot.send_chat_action(chat_id=configWrap.bot.chat_id, action=ChatAction.RECORD_VIDEO)
        # Todo: refactor all timelapse cals
        (video_bio, thumb_bio, width, height, video_path, gcode_name) = cameraWrap.create_timelapse_for_file(lapse_name, info_mess)
        info_mess.edit_text(text="Uploading time-lapse")
        if video_bio.getbuffer().nbytes > 52428800:
            info_mess.edit_text(text=f'Telegram bots have a 50mb filesize restriction, please retrieve the timelapse from the configured folder\n{video_path}')
        else:
            query.bot.send_video(configWrap.bot.chat_id, video=video_bio, thumb=thumb_bio, width=width, height=height, caption=f'time-lapse of {lapse_name}', timeout=120,
                                 disable_notification=notifier.silent_commands)
            query.bot.delete_message(chat_id=configWrap.bot.chat_id, message_id=info_mess.message_id)

        video_bio.close()
        thumb_bio.close()
        query.delete_message()
        check_unfinished_lapses()
    else:
        logger.debug(f"unknown message from inline keyboard query: {query.data}")
        query.delete_message()


def get_gcode_files(update: Update, _: CallbackContext) -> None:
    def create_file_button(element) -> InlineKeyboardButton:
        filename = element['path'] if 'path' in element else element['filename']
        return InlineKeyboardButton(filename, callback_data=hashlib.md5(filename.encode()).hexdigest() + '.gcode')

    update.message.bot.send_chat_action(chat_id=configWrap.bot.chat_id, action=ChatAction.TYPING)
    files_keys = list(map(list, zip(map(create_file_button, klippy.get_gcode_files()))))
    reply_markup = InlineKeyboardMarkup(files_keys)

    update.message.reply_text('Gcode files to print:', reply_markup=reply_markup, disable_notification=notifier.silent_commands, quote=True)


def exec_gcode(update: Update, _: CallbackContext) -> None:
    # maybe use context.args
    message = update.message if update.message else update.effective_message
    if not message.text == '/gcode':
        command = message.text.replace('/gcode ', '')
        klippy.execute_command(command)
    else:
        message.reply_text('No command provided', quote=True)


def get_macros(update: Update, _: CallbackContext) -> None:
    update.effective_message.bot.send_chat_action(chat_id=configWrap.bot.chat_id, action=ChatAction.TYPING)
    files_keys = list(map(list, zip(map(lambda el: InlineKeyboardButton(el, callback_data=f'macroc:{el}' if configWrap.telegram_ui.require_confirmation_macro else f'macro:{el}'), klippy.macros))))
    reply_markup = InlineKeyboardMarkup(files_keys)

    update.effective_message.reply_text('Gcode macros:', reply_markup=reply_markup, disable_notification=notifier.silent_commands, quote=True)


def macros_handler(update: Update, _: CallbackContext) -> None:
    command = update.effective_message.text.replace('/', '').upper()
    if command in klippy.macros_all:
        if configWrap.telegram_ui.require_confirmation_macro:
            update.effective_message.reply_text(f"Execute marco {command}?", reply_markup=confirm_keyboard(f'macro:{command}'), disable_notification=notifier.silent_commands, quote=True)
        else:
            klippy.execute_command(command)
            update.effective_message.reply_text(f"Running macro: {command}", disable_notification=notifier.silent_commands, quote=True)
    else:
        echo_unknown(update, _)


def upload_file(update: Update, _: CallbackContext) -> None:
    update.message.bot.send_chat_action(chat_id=configWrap.bot.chat_id, action=ChatAction.UPLOAD_DOCUMENT)
    doc = update.message.document
    if not doc.file_name.endswith(('.gcode', '.zip')):
        update.message.reply_text(f"unknown filetype in {doc.file_name}", disable_notification=notifier.silent_commands, quote=True)
        return

    try:
        file_byte_array = doc.get_file().download_as_bytearray()
    except BadRequest as badreq:
        update.message.reply_text(f"Bad request: {badreq.message}", disable_notification=notifier.silent_commands, quote=True)
        return

    # Todo: add context managment!
    uploaded_bio = BytesIO()
    uploaded_bio.name = doc.file_name
    uploaded_bio.write(file_byte_array)
    uploaded_bio.seek(0)

    sending_bio = BytesIO()
    if doc.file_name.endswith('.gcode'):
        sending_bio = uploaded_bio
    elif doc.file_name.endswith('.zip'):
        with ZipFile(uploaded_bio) as my_zip_file:
            if len(my_zip_file.namelist()) > 1:
                update.message.reply_text(f"Multiple files in archive {doc.file_name}", disable_notification=notifier.silent_commands, quote=True)
                return

            contained_file = my_zip_file.open(my_zip_file.namelist()[0])
            sending_bio.name = contained_file.name
            sending_bio.write(contained_file.read())
            sending_bio.seek(0)

    if klippy.upload_file(sending_bio):
        filehash = hashlib.md5(doc.file_name.encode()).hexdigest() + '.gcode'
        keyboard = [
            [
                InlineKeyboardButton(emoji.emojize(':robot: print file', use_aliases=True), callback_data=f'print_file:{filehash}'),
                InlineKeyboardButton(emoji.emojize(':cross_mark: do nothing', use_aliases=True), callback_data='do_nothing'),
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        update.message.reply_text(f"Successfully uploaded file: {sending_bio.name}", reply_markup=reply_markup, disable_notification=notifier.silent_commands, quote=True)
    else:
        update.message.reply_text(f"Failed uploading file: {sending_bio.name}", disable_notification=notifier.silent_commands, quote=True)

    uploaded_bio.close()
    sending_bio.close()


def bot_error_handler(_: object, context: CallbackContext) -> None:
    logger.error(msg="Exception while handling an update:", exc_info=context.error)


def create_keyboard():
    if not configWrap.telegram_ui.buttons_default:
        return configWrap.telegram_ui.buttons

    custom_keyboard = []
    if cameraWrap.enabled:
        custom_keyboard.append('/video')
    if psu_power_device:
        custom_keyboard.append('/power')
    if light_power_device:
        custom_keyboard.append('/light')

    keyboard = configWrap.telegram_ui.buttons
    if len(custom_keyboard) > 0:
        keyboard.append(custom_keyboard)
    return keyboard


def help_command(update: Update, _: CallbackContext) -> None:
    update.message.reply_text('The following commands are known:\n\n'
                              '/status - send klipper status\n'
                              '/pause - pause printing\n'
                              '/resume - resume printing\n'
                              '/cancel - cancel printing\n'
                              '/files - list last 5 files(you can start printing one from menu)\n'
                              '/macros - list all visible macros from klipper\n'
                              '/gcode - run any gcode command, spaces are supported (/gcode G28 Z)\n'
                              '/video - will take mp4 video from camera\n'
                              '/power - toggle moonraker power device from config\n'
                              '/light - toggle light\n'
                              '/emergency - emergency stop printing\n'
                              '/bot_restart - restarts the bot service, useful for config updates\n'
                              '/shutdown - shutdown Pi gracefully',
                              quote=True)


def greeting_message():
    response = klippy.check_connection()
    mess = f'Bot online, no moonraker connection!\n {response} \nFailing...' if response else 'Printer online'
    if configWrap.unknown_fields:
        mess += f"\n{configWrap.unknown_fields}"
    reply_markup = ReplyKeyboardMarkup(create_keyboard(), resize_keyboard=True)
    bot_updater.bot.send_message(configWrap.bot.chat_id, text=mess, reply_markup=reply_markup, disable_notification=notifier.silent_status)
    commands = [
        ('help', 'list bot commands'),
        ('status', 'send klipper status'),
        ('pause', 'pause printing'),
        ('resume', 'resume printing'),
        ('cancel', 'cancel printing'),
        ('files', "list last 5 files. you can start printing one from menu"),
        ('macros', 'list all visible macros from klipper'),
        ('gcode', 'run any gcode command, spaces are supported. "gcode G28 Z"'),
        ('video', 'will take mp4 video from camera'),
        ('power', 'toggle moonraker power device from config'),
        ('light', 'toggle light'),
        ('emergency', 'emergency stop printing'),
        ('bot_restart', 'restarts the bot service, useful for config updates'),
        ('shutdown', 'shutdown Pi gracefully')
    ]
    if configWrap.telegram_ui.include_macros_in_command_list:
        commands += list(map(lambda el: (el.lower(), el), klippy.macros))
        if len(commands) >= 100:
            logger.warning("Commands list too large!")
            commands = commands[0:99]
    bot_updater.bot.set_my_commands(commands=commands)
    check_unfinished_lapses()


def start_bot(bot_token, socks):
    request_kwargs = {}
    if socks:
        request_kwargs['proxy_url'] = f'socks5://{socks}'

    updater = Updater(bot_token, workers=4, request_kwargs=request_kwargs)

    dispatcher = updater.dispatcher

    dispatcher.add_handler(MessageHandler(~Filters.chat(configWrap.bot.chat_id), unknown_chat))

    dispatcher.add_handler(CallbackQueryHandler(button_handler))
    dispatcher.add_handler(CommandHandler("help", help_command, run_async=True))
    dispatcher.add_handler(CommandHandler("status", status, run_async=True))
    dispatcher.add_handler(CommandHandler("video", get_video))
    dispatcher.add_handler(CommandHandler("pause", pause_printing))
    dispatcher.add_handler(CommandHandler("resume", resume_printing))
    dispatcher.add_handler(CommandHandler("cancel", cancel_printing))
    dispatcher.add_handler(CommandHandler("power", power))
    dispatcher.add_handler(CommandHandler("light", light_toggle))
    dispatcher.add_handler(CommandHandler("emergency", emergency_stop))
    dispatcher.add_handler(CommandHandler("shutdown", shutdown_host))
    dispatcher.add_handler(CommandHandler("bot_restart", bot_restart))
    dispatcher.add_handler(CommandHandler("files", get_gcode_files, run_async=True))
    dispatcher.add_handler(CommandHandler("macros", get_macros, run_async=True))
    dispatcher.add_handler(CommandHandler("gcode", exec_gcode, run_async=True))

    dispatcher.add_handler(MessageHandler(Filters.command, macros_handler, run_async=True))

    dispatcher.add_handler(MessageHandler(Filters.document & ~Filters.command, upload_file, run_async=True))

    dispatcher.add_handler(MessageHandler(Filters.text & ~Filters.command, echo_unknown))

    dispatcher.add_error_handler(bot_error_handler)

    updater.start_polling()

    return updater


def on_close(_, close_status_code, close_msg):
    logger.info("WebSocket closed")
    if close_status_code or close_msg:
        logger.error("WebSocket close status code: " + str(close_status_code))
        logger.error("WebSocket close message: " + str(close_msg))


def on_error(_, error):
    logger.error(error)


def subscribe(websock):
    subscribe_objects = {
        'print_stats': None,
        'display_status': None,
        'toolhead': ['position'],
        'gcode_move': ['position', 'gcode_position'],
        'virtual_sdcard': ['progress']
    }

    sensors = klippy.prepare_sens_dict_subscribe()
    if sensors:
        subscribe_objects.update(sensors)

    websock.send(
        ujson.dumps({'jsonrpc': '2.0',
                     'method': 'printer.objects.subscribe',
                     'params': {
                         'objects': subscribe_objects
                     },
                     'id': myId}))


def on_open(websock):
    websock.send(
        ujson.dumps({'jsonrpc': '2.0',
                     'method': 'printer.info',
                     'id': myId}))
    websock.send(
        ujson.dumps({'jsonrpc': '2.0',
                     'method': 'machine.device_power.devices',
                     'id': myId}))


def reshedule():
    if not klippy.connected and ws.keep_running:
        on_open(ws)


def stop_all():
    klippy.stop_all()
    notifier.stop_all()
    timelapse.stop_all()


def status_response(status_resp):
    if 'print_stats' in status_resp:
        print_stats = status_resp['print_stats']
        if print_stats['state'] in ['printing', 'paused']:
            klippy.printing = True
            klippy.printing_filename = print_stats['filename']
            klippy.printing_duration = print_stats['print_duration']
            klippy.filament_used = print_stats['filament_used']
            # Todo: maybe get print start time and set start interval for job?
            notifier.add_notifier_timer()
            if not timelapse.manual_mode:
                timelapse.running = True
                # TOdo: manual timelapse start check?

        # Fixme: some logic error with states for klippy.paused and printing
        if print_stats['state'] == "printing":
            klippy.paused = False
            if not timelapse.manual_mode:
                timelapse.paused = False
        if print_stats['state'] == "paused":
            klippy.paused = True
            if not timelapse.manual_mode:
                timelapse.paused = True
    if 'display_status' in status_resp:
        notifier.m117_status = status_resp['display_status']['message']
        klippy.printing_progress = status_resp['display_status']['progress']
    if 'virtual_sdcard' in status_resp:
        klippy.vsd_progress = status_resp['virtual_sdcard']['progress']

    # Todo: add sensors & heaters parsing
    for sens in [key for key in status_resp if 'temperature_sensor' in key]:
        if status_resp[sens]:
            klippy.update_sensror(sens.replace('temperature_sensor ', ''), status_resp[sens])

    for heater in [key for key in status_resp if 'extruder' in key or 'heater_bed' in key or 'heater_generic' in key]:
        if status_resp[heater]:
            klippy.update_sensror(heater.replace('extruder ', '').replace('heater_bed ', '').replace('heater_generic ', ''), status_resp[heater])


# Todo: add command for setting status!
def notify_gcode_reponse(message_params):
    if timelapse.manual_mode:
        if 'timelapse start' in message_params:
            if not klippy.printing_filename:
                klippy.get_status()
            timelapse.clean()
            timelapse.running = True

        if 'timelapse stop' in message_params:
            timelapse.running = False
        if 'timelapse pause' in message_params:
            timelapse.paused = True
        if 'timelapse resume' in message_params:
            timelapse.paused = False
        if 'timelapse create' in message_params:
            timelapse.send_timelapse()
    if 'timelapse photo' in message_params:
        timelapse.take_lapse_photo(manually=True)
    if message_params[0].startswith('tgnotify '):
        notifier.send_notification(message_params[0][9:])
    if message_params[0].startswith('tgnotify_photo '):
        notifier.send_notification_with_photo(message_params[0][15:])
    if message_params[0].startswith('tgalarm '):
        notifier.send_error(message_params[0][8:])
    if message_params[0].startswith('tgalarm_photo '):
        notifier.send_error_with_photo(message_params[0][14:])
    if message_params[0].startswith('tgnotify_status '):
        notifier.tgnotify_status = message_params[0][16:]
    if message_params[0].startswith('set_timelapse_params '):
        timelapse.parse_timelapse_params(message_params[0])
    if message_params[0].startswith('set_notify_params '):
        notifier.parse_notification_params(message_params[0])


def notify_status_update(message_params):
    if 'display_status' in message_params[0]:
        if 'message' in message_params[0]['display_status']:
            notifier.m117_status = message_params[0]['display_status']['message']
        if 'progress' in message_params[0]['display_status']:
            klippy.printing_progress = message_params[0]['display_status']['progress']
            notifier.schedule_notification(progress=int(message_params[0]['display_status']['progress'] * 100))

    if 'toolhead' in message_params[0] and 'position' in message_params[0]['toolhead']:
        # position_z = json_message["params"][0]['toolhead']['position'][2]
        pass
    if 'gcode_move' in message_params[0] and 'position' in message_params[0]['gcode_move']:
        position_z = message_params[0]['gcode_move']['gcode_position'][2]
        klippy.printing_height = position_z
        notifier.schedule_notification(position_z=int(position_z))
        timelapse.take_lapse_photo(position_z)

    if 'virtual_sdcard' in message_params[0] and 'progress' in message_params[0]['virtual_sdcard']:
        klippy.vsd_progress = message_params[0]['virtual_sdcard']['progress']

    if 'print_stats' in message_params[0]:
        parse_print_stats(message_params)

    for sens in [key for key in message_params[0] if 'temperature_sensor' in key]:
        klippy.update_sensror(sens.replace('temperature_sensor ', ''), message_params[0][sens])

    for heater in [key for key in message_params[0] if 'extruder' in key or 'heater_bed' in key or 'heater_generic' in key]:
        klippy.update_sensror(heater.replace('extruder ', '').replace('heater_bed ', '').replace('heater_generic ', ''), message_params[0][heater])


def parse_print_stats(message_params):
    state = ""
    # Fixme:  maybe do not parse without state? history data may not be avaliable
    # Message with filename will be sent before printing is started
    if 'filename' in message_params[0]['print_stats']:
        klippy.printing_filename = message_params[0]['print_stats']['filename']
    if 'filament_used' in message_params[0]['print_stats']:
        klippy.filament_used = message_params[0]['print_stats']['filament_used']
    if 'state' in message_params[0]['print_stats']:
        state = message_params[0]['print_stats']['state']
    # Fixme: reset notify percent & height on finish/cancel/start
    if 'print_duration' in message_params[0]['print_stats']:
        klippy.printing_duration = message_params[0]['print_stats']['print_duration']
    if state == 'printing':
        klippy.paused = False
        if not klippy.printing:
            klippy.printing = True
            notifier.reset_notifications()
            notifier.add_notifier_timer()
            if not klippy.printing_filename:
                klippy.get_status()
            if not timelapse.manual_mode:
                timelapse.clean()
                timelapse.running = True
            notifier.send_print_start_info()

        if not timelapse.manual_mode:
            timelapse.paused = False
    elif state == 'paused':
        klippy.paused = True
        if not timelapse.manual_mode:
            timelapse.paused = True
    # Todo: cleanup timelapse dir on cancel print!
    elif state == 'complete':
        klippy.printing = False
        notifier.remove_notifier_timer()
        if not timelapse.manual_mode:
            timelapse.running = False
            timelapse.send_timelapse()
        # Fixme: add finish printing method in notifier
        notifier.send_print_finish()
    elif state == 'error':
        klippy.printing = False
        timelapse.running = False
        notifier.remove_notifier_timer()
        error_mess = f"Printer state change error: {message_params[0]['print_stats']['state']}\n"
        if 'message' in message_params[0]['print_stats'] and message_params[0]['print_stats']['message']:
            error_mess += f"{message_params[0]['print_stats']['message']}\n"
        notifier.send_error(error_mess)
    elif state == 'standby':
        klippy.printing = False
        notifier.remove_notifier_timer()
        # Fixme: check manual mode
        timelapse.running = False
        notifier.send_notification(f"Printer state change: {message_params[0]['print_stats']['state']} \n")
    elif state:
        logger.error(f"Unknown state: {state}")


def power_device_state(device):
    device_name = device["device"]
    device_state = True if device["status"] == 'on' else False
    if psu_power_device and psu_power_device.name == device_name:
        psu_power_device.device_state = device_state
    if light_power_device and light_power_device.name == device_name:
        light_power_device.device_state = device_state


def websocket_to_message(ws_loc, ws_message):
    json_message = ujson.loads(ws_message)
    logger.debug(ws_message)

    if 'error' in json_message:
        return

    if 'id' in json_message:
        if 'result' in json_message:
            message_result = json_message['result']

            if 'status' in message_result:
                status_response(message_result['status'])
                return

            if 'state' in message_result:
                klippy_state = message_result['state']
                klippy.state = klippy_state
                if klippy_state == 'ready':
                    if ws_loc.keep_running:
                        klippy.connected = True
                        klippy.state_message = ''
                        subscribe(ws_loc)
                        if scheduler.get_job('ws_reschedule'):
                            scheduler.remove_job('ws_reschedule')
                elif klippy_state in ['error', 'shutdown', 'startup']:
                    klippy.connected = False
                    scheduler.add_job(reshedule, 'interval', seconds=2, id='ws_reschedule', replace_existing=True)
                    state_message = message_result['state_message']
                    if not klippy.state_message == state_message and not klippy_state == 'startup':
                        klippy.state_message = state_message
                        notifier.send_error(f"Klippy changed state to {klippy.state}\n{klippy.state_message}")
                else:
                    logger.error(f"UnKnown klippy state: {klippy_state}")
                    klippy.connected = False
                    scheduler.add_job(reshedule, 'interval', seconds=2, id='ws_reschedule', replace_existing=True)
                return

            if 'devices' in message_result:
                for device in message_result['devices']:
                    power_device_state(device)
                return

            # if debug:
            #     bot_updater.bot.send_message(chatId, text=f"{message_result}")

        if 'error' in json_message:
            notifier.send_error(f"{json_message['error']['message']}")

    else:
        message_method = json_message['method']
        if message_method in ["notify_klippy_shutdown", "notify_klippy_disconnected"]:
            logger.warning(f"klippy disconnect detected with message: {json_message['method']}")
            stop_all()
            klippy.connected = False
            scheduler.add_job(reshedule, 'interval', seconds=2, id='ws_reschedule', replace_existing=True)

        if 'params' not in json_message:
            return

        message_params = json_message['params']

        if message_method == 'notify_gcode_response':
            notify_gcode_reponse(message_params)

        if message_method == 'notify_power_changed':
            for device in message_params:
                power_device_state(device)

        if message_method == 'notify_status_update':
            notify_status_update(message_params)


def parselog():
    with open('../telegram.log') as f:
        lines = f.readlines()

    wslines = list(filter(lambda it: ' - {' in it, lines))
    tt = list(map(lambda el: el.split(' - ')[-1].replace('\n', ''), wslines))

    for mes in tt:
        websocket_to_message(ws, mes)
        import time
        time.sleep(0.01)
    print('lalal')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Moonraker Telegram Bot")
    parser.add_argument(
        "-c", "--configfile", default="./telegram.conf",
        metavar='<configfile>',
        help="Location of moonraker telegram bot configuration file")
    system_args = parser.parse_args()
    conf = configparser.ConfigParser(allow_no_value=True, inline_comment_prefixes=(';', '#'))

    # Todo: os.chdir(Path(sys.path[0]).parent.absolute())
    os.chdir(sys.path[0])

    conf.read(system_args.configfile)
    configWrap = ConfigWrapper(conf)

    if not configWrap.bot.log_path == '/tmp':
        Path(configWrap.bot.log_path).mkdir(parents=True, exist_ok=True)

    rotatingHandler = RotatingFileHandler(os.path.join(f'{configWrap.bot.log_path}/', 'telegram.log'), maxBytes=26214400, backupCount=3)
    rotatingHandler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
    logger.addHandler(rotatingHandler)

    if configWrap.bot.debug:
        faulthandler.enable()
        logger.setLevel(logging.DEBUG)
        logging.getLogger('apscheduler').addHandler(rotatingHandler)

    light_power_device = PowerDevice(configWrap.bot.light_device_name, configWrap.bot.host)
    psu_power_device = PowerDevice(configWrap.bot.poweroff_device_name, configWrap.bot.host)

    klippy = Klippy(configWrap, light_power_device, psu_power_device, rotatingHandler)
    cameraWrap = Camera(configWrap, klippy, light_power_device, rotatingHandler)
    bot_updater = start_bot(configWrap.bot.token, configWrap.bot.socks_proxy)
    timelapse = Timelapse(configWrap, klippy, cameraWrap, scheduler, bot_updater.bot, rotatingHandler)
    notifier = Notifier(configWrap, bot_updater.bot, klippy, cameraWrap, scheduler, rotatingHandler)

    scheduler.start()

    greeting_message()

    ws = websocket.WebSocketApp(f"ws://{configWrap.bot.host}/websocket{klippy.one_shot_token}", on_message=websocket_to_message, on_open=on_open, on_error=on_error, on_close=on_close)

    # debug reasons only
    if configWrap.bot.log_parser:
        parselog()

    scheduler.add_job(reshedule, 'interval', seconds=2, id='ws_reschedule', replace_existing=True)

    ws.run_forever(skip_utf8_validation=True)
    logger.info("Exiting! Moonraker connection lost!")

    bot_updater.stop()
