from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from bot import logic
from bot.models import Answer, Event, Kind, Mode, Occurrence, Override, Place, Rsvp, User
from bot.quickparse import ParseError, parse_line, parse_recurrence
from bot.recurrence import describe, expand
from bot.scheduler import due_offsets
from bot.timeutil import parse_duration, parse_when, resolve_tz, to_ts

MSK = "Europe/Moscow"
BER = "Europe/Berlin"
NOW = datetime(2026, 11, 4, 12, 0, tzinfo=ZoneInfo(MSK))  # среда


def ts(s: str, tz: str = MSK) -> int:
    return int(datetime.fromisoformat(s).replace(tzinfo=ZoneInfo(tz)).timestamp())


def daily(**kw) -> Event:
    base = dict(id=1, title="Дейли", kind=Kind.RECURRING, tz=MSK, start_date="2026-11-02", start_time="10:00",
                duration_min=15, organizer_id=1, weekdays=[0, 1, 2, 3, 4], reminders=[10])
    base.update(kw)
    return Event(**base)


def single(**kw) -> Event:
    base = dict(id=2, title="Демо", kind=Kind.SINGLE, tz=MSK, start_date="2026-11-05", start_time="15:00",
                duration_min=60, organizer_id=1, reminders=[1440, 60, 10], participant_ids=[1, 2])
    base.update(kw)
    return Event(**base)


# ---------- время ----------

@pytest.mark.parametrize("text,expected", [
    ("15:30", "2026-11-04 15:30"),
    ("11:00", "2026-11-05 11:00"),        # уже прошло сегодня → завтра
    ("завтра 10:00", "2026-11-05 10:00"),
    ("пт 11:00", "2026-11-06 11:00"),
    ("ср 11:00", "2026-11-11 11:00"),     # сегодня ср, но 11:00 прошло → следующая
    ("05.11 14:00", "2026-11-05 14:00"),
    ("01.01 9:00", "2027-01-01 09:00"),   # дата в прошлом без года → следующий год
])
def test_parse_when(text, expected):
    assert parse_when(text, MSK, NOW).strftime("%Y-%m-%d %H:%M") == expected


def test_parse_when_invalid():
    assert parse_when("когда-нибудь", MSK, NOW) is None
    assert parse_when("31.02 10:00", MSK, NOW) is None


@pytest.mark.parametrize("text,minutes", [("30", 30), ("45м", 45), ("1ч", 60), ("1ч30м", 90), ("1.5ч", 90), ("90 мин", 90)])
def test_parse_duration(text, minutes):
    assert parse_duration(text) == minutes


def test_resolve_tz():
    assert resolve_tz("Москва") == MSK
    assert resolve_tz("europe/berlin") == BER
    assert resolve_tz("UTC+5") == "Etc/GMT-5"
    assert resolve_tz("Марс") is None


# ---------- повторы ----------

def test_daily_weekdays_only():
    occs = expand(daily(), ts("2026-11-02 00:00"), ts("2026-11-09 00:00"))
    assert [datetime.fromtimestamp(o.start, ZoneInfo(MSK)).strftime("%a %H:%M") for o in occs] == \
        ["Mon 10:00", "Tue 10:00", "Wed 10:00", "Thu 10:00", "Fri 10:00"]


def test_biweekly():
    ev = daily(weekdays=[4], interval_weeks=2)
    occs = expand(ev, ts("2026-11-02 00:00"), ts("2026-11-30 00:00"))
    assert [datetime.fromtimestamp(o.start, ZoneInfo(MSK)).day for o in occs] == [6, 20]


def test_recurring_keeps_local_time_across_dst():
    # в Берлине 29.03.2026 переход на летнее время: дейли в 10:00 остаётся в 10:00 по Берлину
    ev = daily(tz=BER, start_date="2026-03-23")
    occs = expand(ev, ts("2026-03-26 00:00", BER), ts("2026-03-31 00:00", BER))
    assert [datetime.fromtimestamp(o.start, ZoneInfo(BER)).strftime("%H:%M") for o in occs] == ["10:00"] * 3


def test_override_move_and_cancel():
    ev = daily()
    tue, wed = ts("2026-11-03 10:00"), ts("2026-11-04 10:00")
    ovs = {tue: Override(1, tue, ts("2026-11-03 12:00")), wed: Override(1, wed, cancelled=True)}
    occs = expand(ev, ts("2026-11-03 00:00"), ts("2026-11-05 00:00"), ovs)
    assert len(occs) == 1
    assert occs[0].key == tue and occs[0].start == ts("2026-11-03 12:00") and occs[0].moved


def test_describe():
    assert describe(daily()) == "по будням"
    assert describe(daily(weekdays=[0, 3])) == "еженедельно: пн, чт"
    assert describe(daily(weekdays=[4], interval_weeks=2)) == "раз в 2 нед.: пт"
    assert describe(single()) == "разовый"


# ---------- быстрый ввод ----------

def test_quick_single():
    d = parse_line("Демо заказчику | 05.11 14:00 | 45м | @Anna @oleg | https://zoom.us/j/1 | Показ MVP", MSK, NOW)
    assert d.title == "Демо заказчику" and not d.recurring
    assert d.start.strftime("%d.%m %H:%M") == "05.11 14:00"
    assert d.duration_min == 45 and d.usernames == ["anna", "oleg"]
    assert d.call_url == "https://zoom.us/j/1" and d.agenda == "Показ MVP"


def test_quick_daily_time_only():
    d = parse_line("Дейли | 10:00 | 15м | по будням", MSK, NOW)
    assert d.recurring and d.weekdays == [0, 1, 2, 3, 4]
    assert d.start.strftime("%a %d %H:%M") == "Thu 05 10:00"  # ближайший будний день после «сейчас»


def test_quick_biweekly():
    d = parse_line("Ретро | пт 16:00 | 1ч | каждые 2 недели", MSK, NOW)
    assert d.recurring and d.weekdays == [4] and d.interval_weeks == 2


def test_quick_errors():
    with pytest.raises(ParseError):
        parse_line("Без времени", MSK, NOW)
    with pytest.raises(ParseError):
        parse_line("X | когда-нибудь", MSK, NOW)


def test_parse_recurrence():
    assert parse_recurrence("каждый пн,ср") == ([0, 2], 1)
    assert parse_recurrence("еженедельно: пн-ср") == ([0, 1, 2], 1)
    assert parse_recurrence("ежедневно") == (list(range(7)), 1)
    assert parse_recurrence("просто текст") is None


# ---------- правила ----------

def test_default_reminders():
    assert logic.default_reminders(Kind.SINGLE, []) == [1440, 60, 10]
    assert logic.default_reminders(Kind.RECURRING, [0, 1, 2, 3, 4]) == [10]
    assert logic.default_reminders(Kind.RECURRING, [2]) == [60, 10]


def test_participants():
    team = [User(1, "A", None, MSK), User(2, "B", None, MSK), User(3, "C", None, MSK, active=False)]
    assert logic.participant_ids(daily(), team) == [1, 2]
    assert logic.participant_ids(single(participant_ids=[2, 3]), team) == [2]
    assert logic.can_edit(single(), 2, team) and not logic.can_edit(single(participant_ids=[1]), 2, team)
    assert logic.can_delete(single(), 1) and not logic.can_delete(single(), 2)


def test_remote_detection():
    office = User(1, "A", None, MSK, Mode.OFFICE)
    remote = User(2, "B", None, MSK, Mode.REMOTE)
    assert not logic.anyone_remote([office], {})
    assert logic.anyone_remote([office], {1: Answer(Rsvp.YES, Place.ONLINE)})   # место из ответа важнее профиля
    assert logic.anyone_remote([remote], {})
    assert not logic.anyone_remote([remote], {2: Answer(Rsvp.YES, Place.OFFICE)})
    assert logic.anyone_remote([remote], {2: Answer(Rsvp.LATE)})               # опоздает, но всё ещё из дома
    assert not logic.anyone_remote([remote], {2: Answer(Rsvp.NO, Place.ONLINE)})


def test_conflicts():
    ev_new = single(id=10, start_date="2026-11-05", start_time="10:05", participant_ids=[1, 3])
    cand = expand(ev_new, 0, 2**40)
    others = [(o, {1, 2}) for o in expand(daily(), ts("2026-11-05 00:00"), ts("2026-11-06 00:00"))]
    conflicts = logic.find_conflicts(cand, {1, 3}, others)
    assert [(c.user_id, c.other.event.title) for c in conflicts] == [(1, "Дейли")]
    # сразу после дейли — не пересекается
    later = expand(single(id=11, start_time="10:15", participant_ids=[1]), 0, 2**40)
    assert logic.find_conflicts(later, {1}, others) == []


# ---------- планировщик ----------

def occ_of(ev: Event) -> Occurrence:
    start = to_ts(datetime.fromisoformat(ev.start_date).date(), datetime.strptime(ev.start_time, "%H:%M").time(), ev.tz)
    return Occurrence(ev, start, start, start + ev.duration_min * 60)


def test_due_offsets():
    occ = occ_of(single())
    start = occ.start
    assert due_offsets(occ, start - 1440 * 60) == [1440]
    assert due_offsets(occ, start - 1440 * 60 - 1) == []
    assert due_offsets(occ, start - 30 * 60) == []
    assert due_offsets(occ, start - 9 * 60) == [10]
    assert due_offsets(occ, start + 60) == [0]                 # на старте шлём только «начинается»
    assert due_offsets(occ, start + 20 * 60) == []             # опоздали больше чем на GRACE — молчим


def test_quick_online():
    d = parse_line("Синк | чт 12:00 | 30м | онлайн | https://meet.google.com/x", MSK, NOW)
    assert d.online and d.call_url == "https://meet.google.com/x" and d.agenda is None


def test_toggle_late():
    assert logic.toggle_late(None) == Answer(Rsvp.LATE)
    assert logic.toggle_late(Answer(Rsvp.LATE)) == Answer(Rsvp.YES)                     # повторное нажатие отменяет
    assert logic.toggle_late(Answer(Rsvp.YES, Place.ONLINE)) == Answer(Rsvp.LATE, Place.ONLINE)  # место сохраняется


def test_online_only_always_shows_link():
    office = User(1, "A", None, MSK, Mode.OFFICE)
    assert logic.show_link(single(online_only=True), [office], {})
    assert not logic.show_link(single(), [office], {})


def test_whole_team():
    team = [User(1, "A", None, MSK), User(2, "B", None, MSK), User(3, "C", None, MSK, active=False)]
    assert logic.whole_team(daily(), team)
    assert logic.whole_team(single(participant_ids=[1, 2]), team)          # неактивный не считается
    assert not logic.whole_team(single(participant_ids=[1]), team)


def test_card_text_structure():
    from bot.render import card_header, card_text
    ev = single(agenda="тест", start_date="2026-11-04", start_time="12:10", duration_min=15)
    occ = expand(ev, 0, 2**40)[0]
    now = ts("2026-11-04 12:05")
    assert card_text(occ, MSK, [MSK], now, card_header("⏰", "через 5 мин")) == (
        "⏰ <b>Демо</b> · через 5 мин\nсегодня, 12:10–12:25\n\n<b>Повестка</b>\nтест")
    # завтра, другой пояс у коллеги, без повестки и без пометки
    occ2 = expand(single(start_date="2026-11-05", start_time="12:10"), 0, 2**40)[0]
    assert card_text(occ2, MSK, [MSK, "Asia/Almaty"], now) == "📅 <b>Демо</b>\nзавтра, 12:10–13:10 · 14:10 Алматы"
