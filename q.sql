SELECT substr(analysis_json, instr(analysis_json, char(34)||"structure"||char(34)), 500) FROM workout_analysis WHERE workout_date="2026-08-25" ORDER BY id DESC LIMIT 2;
