import mysql.connector

from config_db import DB_CONFIG


def get_connection():
    return mysql.connector.connect(**DB_CONFIG)


def get_all_sites():
    conn = get_connection()
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT * FROM amc_sites ORDER BY amc_name")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


def get_sites_by_name(partial_name):
    """Case-insensitive partial match, e.g. '360' or 'abakkus'."""
    conn = get_connection()
    cur = conn.cursor(dictionary=True)
    cur.execute(
        "SELECT * FROM amc_sites WHERE amc_name LIKE %s",
        (f"%{partial_name}%",),
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows
