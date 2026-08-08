from aiogram.fsm.state import State, StatesGroup


class Tos(StatesGroup):
    waiting = State()  # ждём нажатия "Принимаю"


class Buy(StatesGroup):
    choose_country = State()
    choose_year = State()
    choose_payment = State()
    wait_screenshot = State()  # Сбер
    wait_code = State()         # имитация кода подтверждения


class AdminFSM(StatesGroup):
    menu = State()
    add_phone = State()
    add_country = State()
    add_year = State()
    add_price = State()
    add_admin_id = State()
    remove_admin_id = State()
