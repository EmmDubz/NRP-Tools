import sqlite3

DB_FILE = 'fns_bot.db'

def run_migration():
    try:
        con = sqlite3.connect(DB_FILE)
        cur = con.cursor()
        print("Connected to database. Adding 'proposer_nation_name' column to resolutions table...")
        cur.execute("ALTER TABLE resolutions ADD COLUMN proposer_nation_name TEXT")
        con.commit()
        con.close()
        print("Migration successful! Column added.")
    except sqlite3.OperationalError as e:
        if "duplicate column name" in str(e):
            print("Column 'proposer_nation_name' already exists. No changes made.")
        else:
            print(f"An error occurred: {e}")
    except Exception as e:
        print(f"An unexpected error occurred: {e}")

if __name__ == "__main__":
    run_migration()