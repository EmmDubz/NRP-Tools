import sqlite3

DB_FILE = 'fns_bot.db'

def run_migration():
    try:
        con = sqlite3.connect(DB_FILE)
        cur = con.cursor()
        print("Connected to database. Adding 'was_force_closed' column to resolutions table...")
        # We add a default value of 0 (False) so existing rows are valid
        cur.execute("ALTER TABLE resolutions ADD COLUMN was_force_closed INTEGER NOT NULL DEFAULT 0")
        con.commit()
        con.close()
        print("Migration successful! Column added.")
    except sqlite3.OperationalError as e:
        if "duplicate column name" in str(e):
            print("Column 'was_force_closed' already exists. No changes made.")
        else:
            print(f"An error occurred: {e}")
    except Exception as e:
        print(f"An unexpected error occurred: {e}")

if __name__ == "__main__":
    run_migration()