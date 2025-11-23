import sqlite3

conn = sqlite3.connect('users.db')
c = conn.cursor()

for row in c.execute('SELECT id, username, phone, email, created_at FROM users'):
    print(row)

conn.close()