import sqlite3
c = sqlite3.connect('/opt/bot/plugins/chat_stats/stats.db')
total = c.execute('SELECT COUNT(*) FROM messages').fetchone()[0]
days = c.execute('SELECT COUNT(DISTINCT day) FROM messages').fetchone()[0]
types = c.execute('SELECT msg_type, COUNT(*) FROM messages GROUP BY msg_type').fetchall()
print('总记录:', total, '| 天数:', days, '| 类型分布:', types)
