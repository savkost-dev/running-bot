"""Отчёт по разосланным рекомендациям из last_recommendation.

Запуск на сервере:
    /opt/running-bot/venv/bin/python3 scripts/broadcast_report.py            # последняя дата в таблице
    /opt/running-bot/venv/bin/python3 scripts/broadcast_report.py 2026-06-08 # конкретная дата

Отлаживаем внешний вид здесь, затем тот же текст прикрутим к финальному
сообщению вечерней рассылки (_notify_admin в scheduled_evening).
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from database import get_connection

# Поимённую секцию можно отключить, когда юзеров станет много.
DETAILED = True


def build_report(workout_date: str | None = None) -> str:
    with get_connection() as conn:
        # Дата по умолчанию — самая свежая в таблице (последняя рассылка).
        if not workout_date:
            row = conn.execute(
                "SELECT workout_date FROM last_recommendation "
                "WHERE workout_date != '' ORDER BY workout_date DESC LIMIT 1"
            ).fetchone()
            workout_date = row[0] if row else None

        if not workout_date:
            return "Нет сохранённых рекомендаций."

        rows = conn.execute("""
            SELECT lr.recommended_group, lr.evening_recovery_score, lr.lowered_by_recovery,
                   u.name
            FROM last_recommendation lr
            LEFT JOIN users u ON u.id = lr.user_id
            WHERE lr.workout_date = ?
            ORDER BY CAST(lr.recommended_group AS REAL), u.name
        """, (workout_date,)).fetchall()

    if not rows:
        return f"Нет рекомендаций за {workout_date}."

    # Группировка: номер группы → список (имя, recovery, lowered)
    groups: dict[str, list] = {}
    for grp, rec_score, lowered, name in rows:
        grp = (grp or "?").strip() or "?"
        groups.setdefault(grp, []).append((name or "—", rec_score, bool(lowered)))

    def _grp_key(g: str):
        try:
            return (0, float(g))
        except ValueError:
            return (1, g)

    ordered = sorted(groups.items(), key=lambda kv: _grp_key(kv[0]))

    lines = [f"📊 Рекомендации за {workout_date} (всего {len(rows)})", ""]

    # Сводка по группам
    summary = " · ".join(f"гр{g}: {len(members)}" for g, members in ordered)
    lines.append(summary)

    # Поимённо
    if DETAILED:
        lines.append("")
        for g, members in ordered:
            names = []
            for name, rec_score, lowered in members:
                tag = ""
                if rec_score is not None:
                    tag = f" (rec={rec_score}{'↓' if lowered else ''})"
                names.append(f"{name}{tag}")
            lines.append(f"гр{g} — " + ", ".join(names))

    return "\n".join(lines)


if __name__ == "__main__":
    date_arg = sys.argv[1] if len(sys.argv) > 1 else None
    print(build_report(date_arg))
