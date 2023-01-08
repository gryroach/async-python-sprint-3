import logging
import sys
import asyncio
import json
import functools

from datetime import datetime, timedelta
from pytz import timezone
from sqlite3 import connect, PARSE_DECLTYPES, PARSE_COLNAMES
from asyncio.streams import StreamReader, StreamWriter
from concurrent.futures import ThreadPoolExecutor

from config import (
    DB_NAME, TZ, HOST, LIMIT_SHOW_MESSAGES, LIFETIME_MESSAGES,
    UPDATE_PERIOD, LIMIT_MESSAGES
)


logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
logger.addHandler(logging.StreamHandler(stream=sys.stdout))


class Server:
    def __init__(self, host=HOST[0], port=HOST[1]):
        self.host: str = host
        self.port: int = port
        self.users: dict[str, StreamWriter] = dict()
        self.db_executor = ThreadPoolExecutor(1)

    def listen(self):
        loop = asyncio.get_event_loop()
        main_task = loop.create_task(self.main())
        delete_messages_task = loop.create_task(self.delete_old_messages())
        reset_limit_task = loop.create_task(self.reset_limit_messages())
        loop.run_until_complete(asyncio.wait([
            main_task, delete_messages_task, reset_limit_task
        ]))

    async def main(self):
        srv = await asyncio.start_server(
            self.client_connected, self.host, self.port)
        async with srv:
            await srv.serve_forever()

    async def client_connected(
            self, reader: StreamReader, writer: StreamWriter):
        address = writer.get_extra_info('peername')
        logger.info('Start serving %s', address)

        while True:
            data = await reader.read(1024)
            await self.process_data(data, writer)
            if not data:
                break

        logger.info('Stop serving %s', address)
        writer.close()

    async def send_to_all(self, self_writer, sender, message):
        if isinstance(message, bytes):
            message = message.decode()
        for w in self.users.values():
            if w != self_writer:
                w.write(
                    f'{datetime.now(timezone(TZ))} {sender}: '
                    f'{message}\n'.encode()
                )
                await w.drain()

    async def send_to_one(self, sender, message, receiver):
        for username, w in self.users.items():
            if username == receiver:
                w.write(
                    f'{datetime.now(timezone(TZ))} {sender}: '
                    f'{message}\n'.encode()
                )
                await w.drain()

    async def send_hello(self, self_writer, sender):
        for w in self.users.values():
            if w != self_writer:
                w.write(f'New guest in the chat! - {sender}\n'.encode())
                await w.drain()

    async def process_data(self, data: bytes, writer: StreamWriter):
        data: dict = json.loads(data.decode())
        target = data['target']
        user = data['username']
        count_messages = 0

        if not target == 'hello':
            await self.store_message(data)

        exist_user = await self.get_user(user)
        if not exist_user:
            await self.reg_user(user)
            reg_date = datetime.now(timezone(TZ))
        else:
            reg_date = exist_user[1]
            count_messages = exist_user[2]

        if target == 'hello':
            await self.send_available_messages(user, writer, reg_date)
            self.users[user] = writer
            await self.send_hello(writer, user)
        elif target == 'all':
            if count_messages >= LIMIT_MESSAGES:
                await self.send_limit_warning(writer)
            else:
                await self.send_to_all(writer, user, data['message'])
                await self.append_count_message(user, count_messages)
        elif target == 'one_to_one':
            await self.send_to_one(
                user, data['message'], data['receiver']
            )

    async def send_available_messages(
            self, user: str, writer: StreamWriter, reg_date: datetime):
        loop = asyncio.get_event_loop()
        messages = await loop.run_in_executor(
            self.db_executor,
            functools.partial(
                self.__get_available_messages,
                receiver=user,
                reg_date=reg_date
            )
        )
        for m in messages:
            message = f'{m[2]} {m[1]}: {m[0]}\n'.encode()
            writer.write(message)
            await writer.drain()

    async def store_message(self, data: dict):
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            self.db_executor,
            functools.partial(
                self.__create_record_in_db,
                message=data['message'],
                sender=data['username'],
                receiver=data['receiver']
            )
        )

    async def reg_user(self, user):
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            self.db_executor,
            functools.partial(
                self.__create_user_db_record,
                user=user
            )
        )

    async def get_user(self, username):
        loop = asyncio.get_event_loop()
        user = await loop.run_in_executor(
            self.db_executor,
            functools.partial(
                self.__get_user,
                username=username
            )
        )
        return user

    async def delete_old_messages(self):
        loop = asyncio.get_event_loop()
        while True:
            await loop.run_in_executor(
                self.db_executor,
                self.__delete_old_messages
            )
            await asyncio.sleep(60)

    async def append_count_message(self, username: str, count_messages: int):
        count_messages += 1
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            self.db_executor,
            functools.partial(
                self.__append_count_message,
                username=username,
                count_messages=count_messages
            )
        )

    async def reset_limit_messages(self):
        loop = asyncio.get_event_loop()
        while True:
            await loop.run_in_executor(
                self.db_executor,
                self.__reset_limits
            )
            await asyncio.sleep(60 * UPDATE_PERIOD)

    @staticmethod
    async def send_limit_warning(writer: StreamWriter):
        writer.write('You have reached the limit for sending messages '
                     'to the general chat\n'.encode())
        await writer.drain()

    @staticmethod
    def __create_record_in_db(message, sender, receiver):
        connection = None
        receiver = receiver or 'all'
        try:
            connection = connect(
                DB_NAME, detect_types=PARSE_DECLTYPES | PARSE_COLNAMES
            )
            cursor = connection.cursor()

            store_message_query = '''
                INSERT INTO main.messages(
                    message, sender, receiver, send_date)
                    VALUES (?, ?, ?, ?);
            '''
            cursor.execute(
                store_message_query,
                (
                    message, sender, receiver,
                    datetime.now(timezone(TZ))
                )
            )
            connection.commit()
        except Exception as er:
            logger.error(f'DB error - record message: {er}')
        finally:
            if connection:
                connection.close()

    @staticmethod
    def __get_available_messages(receiver, reg_date):
        messages = []
        connection = None
        try:
            connection = connect(
                DB_NAME, detect_types=PARSE_DECLTYPES | PARSE_COLNAMES
            )
            cursor = connection.cursor()
            get_message_query = '''
                SELECT *
                FROM (
                    SELECT 
                        message,
                        sender,
                        send_date
                    FROM main.messages
                    WHERE receiver in ('all', ?)
                        AND send_date >= ?
                    ORDER BY send_date
                )
                UNION
                SELECT *
                FROM (
                    SELECT 
                        message,
                        sender,
                        send_date
                    FROM main.messages
                    WHERE receiver in ('all', ?)
                        AND send_date <= ?
                    ORDER BY send_date
                    LIMIT {0}
                )
                ORDER BY send_date;
            '''.format(LIMIT_SHOW_MESSAGES)
            messages = cursor.execute(
                get_message_query, (receiver, reg_date, receiver, reg_date)
            ).fetchall()
        except Exception as er:
            logger.error(f'DB error - get messages: {er}')
        finally:
            if connection:
                connection.close()
        return messages

    @staticmethod
    def __create_user_db_record(user):
        connection = None
        try:
            connection = connect(
                DB_NAME, detect_types=PARSE_DECLTYPES | PARSE_COLNAMES
            )
            cursor = connection.cursor()

            store_message_query = '''
                        INSERT INTO main.registrations(
                            username, reg_date)
                            VALUES (?, ?);
                    '''
            cursor.execute(
                store_message_query,
                (
                    user, datetime.now(timezone(TZ))
                )
            )
            connection.commit()
        except Exception as er:
            logger.error(f'DB error - registration: {er}')
        finally:
            if connection:
                connection.close()

    @staticmethod
    def __get_user(username):
        connection = None
        user = None
        try:
            connection = connect(
                DB_NAME, detect_types=PARSE_DECLTYPES | PARSE_COLNAMES
            )
            cursor = connection.cursor()
            get_user_query = '''
                SELECT 
                    r.username,
                    r.reg_date,
                    r.count_messages
                FROM main.registrations r
                WHERE r.username = ?;
            '''
            user = cursor.execute(
                get_user_query, (username,)
            ).fetchone()
        except Exception as er:
            logger.error(f'DB error - get user: {er}')
        finally:
            if connection:
                connection.close()
        return user

    @staticmethod
    def __delete_old_messages():
        connection = None
        deadline_date = datetime.now(timezone(TZ)) - timedelta(
            minutes=LIFETIME_MESSAGES)
        try:
            connection = connect(
                DB_NAME, detect_types=PARSE_DECLTYPES | PARSE_COLNAMES
            )
            cursor = connection.cursor()
            delete_message_query = f'''
                DELETE FROM main.messages WHERE send_date < '{deadline_date}';
            '''
            cursor.execute(delete_message_query)
            connection.commit()
        except Exception as er:
            logger.error(f'DB error - deleting messages: {er}')
        finally:
            if connection:
                connection.close()

    @staticmethod
    def __append_count_message(username, count_messages):
        connection = None
        try:
            connection = connect(
                DB_NAME, detect_types=PARSE_DECLTYPES | PARSE_COLNAMES
            )
            cursor = connection.cursor()

            append_count_query = ''' 
                UPDATE main.registrations
                SET count_messages = ?
                WHERE username = ?
            '''
            cursor.execute(
                append_count_query,
                (
                    count_messages, username
                )
            )
            connection.commit()
        except Exception as er:
            logger.error(f'DB error - updating count messages: {er}')
        finally:
            if connection:
                connection.close()

    @staticmethod
    def __reset_limits():
        connection = None
        try:
            connection = connect(
                DB_NAME, detect_types=PARSE_DECLTYPES | PARSE_COLNAMES
            )
            cursor = connection.cursor()

            reset_limit_query = ''' 
                UPDATE main.registrations
                SET count_messages = 0
            '''
            cursor.execute(
                reset_limit_query
            )
            connection.commit()
        except Exception as er:
            logger.error(f'DB error - reset limits: {er}')
        finally:
            if connection:
                connection.close()
