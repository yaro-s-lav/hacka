import re

_DIGITS = ('ноль', 'один', 'два', 'три', 'четыре', 'пять', 'шесть', 'семь', 'восемь', 'девять')
_TEENS = ('десять', 'одиннадцать', 'двенадцать', 'тринадцать', 'четырнадцать',
          'пятнадцать', 'шестнадцать', 'семнадцать', 'восемнадцать', 'девятнадцать')
_TENS = ('', '', 'двадцать', 'тридцать', 'сорок', 'пятьдесят', 'шестьдесят',
         'семьдесят', 'восемьдесят', 'девяносто')
_HUNDREDS = ('', 'сто', 'двести', 'триста', 'четыреста', 'пятьсот', 'шестьсот',
             'семьсот', 'восемьсот', 'девятьсот')
_PHONE = re.compile(r'(?<!\w)(\+7|8)[\s(.-]*(\d{3})[\s).\-]*(\d{3})[\s.\-]*(\d{2})[\s.\-]*(\d{2})(?!\w)')
_ADDRESS_NUMBER = re.compile(
    r'\b(дом|квартира|этаж|подъезд|корпус|строение|служба)\s+(\d{1,4})(?![\d/а-яёa-z])', re.I)


def number_words(value: int) -> str:
    if value < 10:
        return _DIGITS[value]
    if value < 20:
        return _TEENS[value - 10]
    if value < 100:
        return _TENS[value // 10] + (f' {_DIGITS[value % 10]}' if value % 10 else '')
    if value < 1000:
        return _HUNDREDS[value // 100] + (f' {number_words(value % 100)}' if value % 100 else '')
    thousands, rest = divmod(value, 1000)
    prefix = 'одна' if thousands == 1 else 'две' if thousands == 2 else number_words(thousands)
    form = 'тысяча' if thousands % 10 == 1 and thousands % 100 != 11 else (
        'тысячи' if thousands % 10 in (2, 3, 4) and thousands % 100 not in (12, 13, 14) else 'тысяч'
    )
    return f'{prefix} {form}' + (f' {number_words(rest)}' if rest else '')


def prepare_number_text(text: str) -> str:
    def phone(match):
        prefix = 'плюс семь' if match[1] == '+7' else 'восемь'
        return prefix + ', ' + ', '.join(' '.join(_DIGITS[int(d)] for d in group) for group in match.groups()[1:])
    text = _PHONE.sub(phone, text)

    def address_number(match):
        return f'{match[1]} {number_words(int(match[2]))}'

    return _ADDRESS_NUMBER.sub(address_number, text)
