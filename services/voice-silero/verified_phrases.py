import re

_PHONE_GENITIVE = re.compile(r'(\bс\s+(?:того|этого)\s+же\s+)(номера)\b', re.I)
_ON_LINE = re.compile(r'\b(на)\s+(связи)\b', re.I)
_ATTACKERS = re.compile(r'\bнападавших\b', re.I)


def correct_verified_phrases(text: str) -> str:
    def replace(match):
        word = match[2]
        stressed = 'Н+ОМЕРА' if word.isupper() else 'Н+омера' if word[0].isupper() else 'н+омера'
        return match[1] + stressed
    text = _PHONE_GENITIVE.sub(replace, text)

    def on_line(match):
        first = match[1]
        if first.isupper():
            first = first.capitalize()
        return f'{first} св+язи'

    text = _ON_LINE.sub(on_line, text)

    def attackers(match):
        return 'НАПАД+АВШИХ' if match[0].isupper() else 'Напад+авших' if match[0][0].isupper() else 'напад+авших'

    return _ATTACKERS.sub(attackers, text)
