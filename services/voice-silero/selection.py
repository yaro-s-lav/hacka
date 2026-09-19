import re

MALE_NAMES = set('иван алексей александр андрей антон артем артём борис вадим валерий василий виктор виталий владимир вячеслав геннадий георгий григорий данил даниил денис дмитрий евгений егор игорь илья кирилл константин лев леонид максим михаил никита николай олег павел петр пётр роман руслан семен семён сергей станислав степан тимофей федор фёдор юрий ярослав'.split())
FEMALE_NAMES = set('александра алина анастасия анна валентина валерия вера виктория галина дарья евгения екатерина елена елизавета жанна зинаида инна ирина ксения лариса лидия любовь людмила маргарита марина мария надежда наталья наталия нина оксана ольга полина светлана софия софья тамара татьяна юлия яна'.split())


def caller_gender(caller_name='', text=''):
    match = re.search(r'(?:меня зовут|мое имя|моё имя)\s+([^.!?\n,]+)', text, re.I) if not caller_name.strip() else None
    parts = re.findall(r'[а-яё]+', (match.group(1) if match else caller_name).lower())
    if any(p.endswith(('овна', 'евна', 'ична')) or p == 'кызы' for p in parts): return 'female'
    if any(p.endswith(('ович', 'евич', 'ьич')) or p == 'оглы' for p in parts): return 'male'
    genders = {'female' if p in FEMALE_NAMES else 'male' for p in parts if p in FEMALE_NAMES or p in MALE_NAMES}
    return next(iter(genders)) if len(genders) == 1 else None
