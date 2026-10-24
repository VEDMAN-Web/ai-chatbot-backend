import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

conn = psycopg2.connect(
    host='127.0.0.1',
    port=5432,
    user='postgres',
    password='CHANGE_ME',
    dbname='postgres'
)
conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
cur = conn.cursor()

cur.execute("SELECT 1 FROM pg_database WHERE datname = 'tajlandia_db'")
exists = cur.fetchone()
if not exists:
    cur.execute("CREATE DATABASE tajlandia_db")
    print("Database tajlandia_db created successfully!")
else:
    print("Database tajlandia_db already exists.")

cur.close()
conn.close()
