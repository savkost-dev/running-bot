"""Разведка: run_kind в recommendation_history — число записей и последняя дата. Путь к базе — аргументом."""
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
for r in con.execute("SELECT run_kind, COUNT(*), MAX(workout_date) FROM recommendation_history GROUP BY run_kind"):
    print(r)
