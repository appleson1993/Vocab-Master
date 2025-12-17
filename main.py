import sqlite3
import random
import json
import urllib.request
import urllib.error
from flask import Flask, render_template, request, jsonify

app = Flask(__name__)
DB_NAME = "vocab.db"

# 初始化資料庫
def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    
    # 1. 建立單字集表
    c.execute('''CREATE TABLE IF NOT EXISTS word_sets 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                  name TEXT NOT NULL UNIQUE)''')
    
    # 確保有預設單字集
    c.execute('SELECT count(*) FROM word_sets')
    if c.fetchone()[0] == 0:
        c.execute("INSERT INTO word_sets (name) VALUES (?)", ('預設單字集',))

    # 2. 建立單字表 (如果不存在)
    c.execute('''CREATE TABLE IF NOT EXISTS words 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                  term TEXT NOT NULL, 
                  definition TEXT NOT NULL, 
                  example TEXT)''')
    
    # 檢查 words 表是否有 set_id 欄位，沒有則新增
    c.execute("PRAGMA table_info(words)")
    columns = [info[1] for info in c.fetchall()]
    if 'set_id' not in columns:
        # 取得預設單字集的 ID (通常是 1)
        c.execute("SELECT id FROM word_sets ORDER BY id ASC LIMIT 1")
        default_set_id = c.fetchone()[0]
        try:
            c.execute(f"ALTER TABLE words ADD COLUMN set_id INTEGER DEFAULT {default_set_id}")
            conn.commit() # 確保 ALTER TABLE 立即生效
        except sqlite3.OperationalError as e:
            print(f"Migration warning: {e}")
    
    # 3. 建立設定表
    c.execute('''CREATE TABLE IF NOT EXISTS settings 
                 (key TEXT PRIMARY KEY, value TEXT)''')
    
    # 4. 建立錯題表
    c.execute('''CREATE TABLE IF NOT EXISTS mistakes 
                 (word_id INTEGER PRIMARY KEY, 
                  count INTEGER DEFAULT 1,
                  last_reviewed TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                  FOREIGN KEY(word_id) REFERENCES words(id) ON DELETE CASCADE)''')
    
    # 確保有預設模型
    c.execute("SELECT value FROM settings WHERE key='model'")
    if not c.fetchone():
        c.execute("INSERT INTO settings (key, value) VALUES (?, ?)", 
                  ('model', 'google/gemini-2.0-flash-lite-preview-02-05:free'))
    
    conn.commit()
    conn.close()

# 連線資料庫 helper
def get_db_connection():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/add_word', methods=['POST'])
def add_word():
    data = request.json
    set_id = data.get('set_id', 1)
    conn = get_db_connection()
    conn.execute('INSERT INTO words (term, definition, example, set_id) VALUES (?, ?, ?, ?)',
                 (data['term'], data['definition'], data['example'], set_id))
    conn.commit()
    conn.close()
    return jsonify({'status': 'success'})

@app.route('/add_words_bulk', methods=['POST'])
def add_words_bulk():
    data = request.json
    words = data.get('words', [])
    set_id = data.get('set_id', 1)
    
    if not words:
        return jsonify({'status': 'error', 'message': 'No words provided'})
    
    conn = get_db_connection()
    try:
        c = conn.cursor()
        # 過濾掉格式不完整的資料 (至少要有 term 和 definition)
        valid_words = [
            (w['term'], w['definition'], w.get('example', ''), set_id) 
            for w in words 
            if w.get('term') and w.get('definition')
        ]
        
        if not valid_words:
             return jsonify({'status': 'error', 'message': 'No valid words to add'})

        c.executemany('INSERT INTO words (term, definition, example, set_id) VALUES (?, ?, ?, ?)', valid_words)
        conn.commit()
        count = c.rowcount
    except Exception as e:
        conn.rollback()
        return jsonify({'status': 'error', 'message': str(e)})
    finally:
        conn.close()
        
    return jsonify({'status': 'success', 'count': count})

@app.route('/record_mistake', methods=['POST'])
def record_mistake():
    data = request.json
    word_id = data.get('word_id')
    if not word_id:
        return jsonify({'status': 'error', 'message': 'Word ID required'})
    
    conn = get_db_connection()
    try:
        # Insert or Update count
        conn.execute('''INSERT INTO mistakes (word_id, count, last_reviewed) 
                        VALUES (?, 1, CURRENT_TIMESTAMP) 
                        ON CONFLICT(word_id) 
                        DO UPDATE SET count = count + 1, last_reviewed = CURRENT_TIMESTAMP''', 
                     (word_id,))
        conn.commit()
        return jsonify({'status': 'success'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})
    finally:
        conn.close()

@app.route('/get_quiz')
def get_quiz():
    set_id = request.args.get('set_id')
    conn = get_db_connection()
    
    # 取得單字
    if set_id == 'mistakes':
        # Join mistakes table
        all_words = conn.execute('''
            SELECT w.* FROM words w 
            JOIN mistakes m ON w.id = m.word_id 
            ORDER BY m.last_reviewed ASC
        ''').fetchall()
    elif set_id and set_id != 'all':
        all_words = conn.execute('SELECT * FROM words WHERE set_id = ?', (set_id,)).fetchall()
    else:
        all_words = conn.execute('SELECT * FROM words').fetchall()
    
    conn.close()

    if len(all_words) < 4:
        # If mistakes mode has few words, we might need to supplement with random words? 
        # Or just return error. For now, return error but maybe handle gracefully in UI.
        if set_id == 'mistakes':
             return jsonify({'error': '錯題本單字不足 4 個，請先多練習累積錯題！'})
        return jsonify({'error': 'Not enough words to generate quiz (need at least 4)'})

    # 1. 選出正確答案
    correct_word = random.choice(all_words)
    
    # 2. 選出 3 個干擾項
    # Distractors should come from ALL words in DB to ensure difficulty
    conn = get_db_connection()
    all_db_words = conn.execute('SELECT * FROM words').fetchall()
    conn.close()
    
    # Filter out correct word
    candidates = [w for w in all_db_words if w['id'] != correct_word['id']]
    if len(candidates) < 3:
         return jsonify({'error': 'Total database words too few for distractors'})
         
    distractors = random.sample(candidates, 3)
    
    # 3. 混合選項
    options = distractors + [correct_word]
    random.shuffle(options)

    return jsonify({
        'question': {
            'term': correct_word['term'],
            'definition': correct_word['definition'],
            'example': correct_word['example']
        },
        'options': [{'id': w['id'], 'term': w['term'], 'definition': w['definition']} for w in options],
        'correct_id': correct_word['id']
    })

# --- 單字集 API ---
@app.route('/api/sets', methods=['GET'])
def get_sets():
    conn = get_db_connection()
    sets = conn.execute('SELECT * FROM word_sets').fetchall()
    conn.close()
    return jsonify([{'id': row['id'], 'name': row['name']} for row in sets])

@app.route('/api/sets', methods=['POST'])
def create_set():
    data = request.json
    name = data.get('name')
    if not name:
        return jsonify({'status': 'error', 'message': 'Name is required'})
    
    conn = get_db_connection()
    try:
        conn.execute('INSERT INTO word_sets (name) VALUES (?)', (name,))
        conn.commit()
        return jsonify({'status': 'success'})
    except sqlite3.IntegrityError:
        return jsonify({'status': 'error', 'message': 'Set name already exists'})
    finally:
        conn.close()

@app.route('/api/sets/<int:set_id>', methods=['DELETE'])
def delete_set(set_id):
    conn = get_db_connection()
    # 刪除該集合下的所有單字
    conn.execute('DELETE FROM words WHERE set_id = ?', (set_id,))
    # 刪除集合本身
    conn.execute('DELETE FROM word_sets WHERE id = ?', (set_id,))
    conn.commit()
    conn.close()
    return jsonify({'status': 'success'})
# ------------------

@app.route('/settings', methods=['GET', 'POST'])
def settings():
    conn = get_db_connection()
    if request.method == 'POST':
        data = request.json
        if 'api_key' in data:
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('api_key', ?)", (data['api_key'],))
        if 'model' in data:
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('model', ?)", (data['model'],))
        conn.commit()
        conn.close()
        return jsonify({'status': 'success'})
    else:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        conn.close()
        return jsonify({row['key']: row['value'] for row in rows})

@app.route('/ai_generate_bulk', methods=['POST'])
def ai_generate_bulk():
    data = request.json
    words = data.get('words', [])
    
    # 從資料庫讀取設定
    conn = get_db_connection()
    settings_rows = conn.execute("SELECT key, value FROM settings").fetchall()
    conn.close()
    settings = {row['key']: row['value'] for row in settings_rows}
    
    api_key = settings.get('api_key')
    model = settings.get('model', 'google/gemini-2.0-flash-lite-preview-02-05:free')
    set_id = data.get('set_id', 1)
    
    if not words:
        return jsonify({'status': 'error', 'message': 'No words provided'})
    if not api_key:
        return jsonify({'status': 'error', 'message': 'API Key 未設定，請至設定頁面輸入'})

    # 1. Call OpenRouter API
    try:
        prompt = f"""
        You are a vocabulary assistant. For the following English words, provide the Traditional Chinese definition and a simple English example sentence.
        
        Words: {', '.join(words)}
        
        Return ONLY a raw JSON array of objects (no markdown formatting). Each object must have these keys:
        - "term": The English word
        - "definition": Traditional Chinese definition (keep it concise)
        - "example": A simple English example sentence
        """
        
        req_body = {
            "model": model,
            "messages": [
                {"role": "user", "content": prompt}
            ]
        }
        
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/chat/completions",
            data=json.dumps(req_body).encode('utf-8'),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "http://localhost:5000",
                "X-Title": "Vocab Master"
            }
        )
        
        with urllib.request.urlopen(req) as response:
            resp_data = json.loads(response.read().decode('utf-8'))
            
        # Parse AI response
        ai_content = resp_data['choices'][0]['message']['content']
        # Clean up markdown code blocks if present
        if ai_content.startswith('```json'):
            ai_content = ai_content[7:]
        if ai_content.startswith('```'):
            ai_content = ai_content[3:]
        if ai_content.endswith('```'):
            ai_content = ai_content[:-3]
            
        generated_words = json.loads(ai_content.strip())

    except urllib.error.HTTPError as e:
        error_body = e.read().decode('utf-8')
        print(f"OpenRouter API Error: {error_body}") # Log to console for debugging
        return jsonify({'status': 'error', 'message': f'AI API Error ({e.code}): {error_body}'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': f'AI Generation failed: {str(e)}'})

    # 2. Save to Database
    conn = get_db_connection()
    try:
        c = conn.cursor()
        valid_words = [
            (w['term'], w['definition'], w['example'], set_id) 
            for w in generated_words 
            if w.get('term') and w.get('definition')
        ]
        
        if not valid_words:
             return jsonify({'status': 'error', 'message': 'AI returned invalid format'})

        c.executemany('INSERT INTO words (term, definition, example, set_id) VALUES (?, ?, ?, ?)', valid_words)
        conn.commit()
        count = c.rowcount
    except Exception as e:
        conn.rollback()
        return jsonify({'status': 'error', 'message': str(e)})
    finally:
        conn.close()

    return jsonify({'status': 'success', 'count': count, 'data': generated_words})

@app.route('/analyze_mistakes', methods=['POST'])
def analyze_mistakes():
    data = request.json
    mistakes = data.get('mistakes', [])
    
    if not mistakes:
        return jsonify({'status': 'error', 'message': 'No mistakes provided'})

    # 從資料庫讀取設定
    conn = get_db_connection()
    settings_rows = conn.execute("SELECT key, value FROM settings").fetchall()
    conn.close()
    settings = {row['key']: row['value'] for row in settings_rows}
    
    api_key = settings.get('api_key')
    model = settings.get('model', 'google/gemini-2.0-flash-lite-preview-02-05:free')
    
    if not api_key:
        return jsonify({'status': 'error', 'message': 'API Key 未設定'})

    try:
        # 建構錯誤題目描述
        mistake_desc = []
        for m in mistakes:
            mistake_desc.append(f"Word: {m['term']}\nCorrect Definition: {m['definition']}\nUser's Wrong Choice: {m['wrong_choice']}")
            
        prompt = f"""
        The user took a vocabulary quiz and made the following mistakes. 
        Please analyze these mistakes and provide specific advice for improvement.
        For each word, explain the nuance or why the user might have been confused.
        Finally, give a short encouraging summary.
        
        Mistakes:
        {chr(10).join(mistake_desc)}
        
        Output in Traditional Chinese (繁體中文).
        """
        
        req_body = {
            "model": model,
            "messages": [
                {"role": "user", "content": prompt}
            ]
        }
        
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/chat/completions",
            data=json.dumps(req_body).encode('utf-8'),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "http://localhost:5000",
                "X-Title": "Vocab Master"
            }
        )
        
        with urllib.request.urlopen(req) as response:
            resp_data = json.loads(response.read().decode('utf-8'))
            
        analysis = resp_data['choices'][0]['message']['content']
        return jsonify({'status': 'success', 'analysis': analysis})

    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})

if __name__ == '__main__':
    init_db()
    app.run(debug=True, host='0.0.0.0', port=5000, load_dotenv=False)