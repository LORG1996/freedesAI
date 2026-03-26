import os
import re
import pickle
import requests
import torch
import json
import streamlit as st
from PIL import Image
import pythoncom
import win32com.client  # Для роботи з ярликами Windows

# Моделі та нейромережі
from ultralytics import YOLO
from sentence_transformers import SentenceTransformer, util
from transformers import AutoImageProcessor, AutoModel

# Компоненти інтерфейсу
from streamlit_cropper import st_cropper
from streamlit_paste_button import paste_image_button

# --- 1. КОНФІГУРАЦІЯ ---
os.environ["HF_TOKEN"] = "ВАШ ТОКЕН"
st.set_page_config(page_title="Freedes AI render search", layout="wide")

DATABASE_FOLDER = 'my_renders'
CACHE_FILE = 'embeddings_cache_ultra.pkl'
MODEL_CLIP = 'clip-ViT-L-14'
MODEL_DINO = 'facebook/dinov2-base'

if not os.path.exists(DATABASE_FOLDER):
    os.makedirs(DATABASE_FOLDER)

# --- 2. ДОПОМІЖНІ ФУНКЦІЇ ---

def get_shortcut_target(shortcut_path):
    """Отримує реальний шлях з Windows .lnk файлу"""
    pythoncom.CoInitialize() 
    try:
        shell = win32com.client.Dispatch("WScript.Shell")
        shortcut = shell.CreateShortCut(os.path.abspath(shortcut_path))
        return shortcut.TargetPath if shortcut.TargetPath else None
    except:
        return None

@st.cache_resource
def load_models():
    detector = YOLO('yolov8n.pt')
    clip_model = SentenceTransformer(MODEL_CLIP)
    dino_processor = AutoImageProcessor.from_pretrained(MODEL_DINO)
    dino_model = AutoModel.from_pretrained(MODEL_DINO)
    return detector, clip_model, dino_processor, dino_model

detector, clip_model, dino_processor, dino_model = load_models()

def get_miro_images(board_id, api_token):
    """Завантаження ОРИГІНАЛІВ (HD) з Miro з ПОВНИМ логуванням у консоль"""
    headers = {"Authorization": f"Bearer {api_token}", "Accept": "application/json"}
    # Початковий URL для отримання списку ітемів
    url = f"https://api.miro.com/v2/boards/{board_id}/items?type=image&limit=50"
    
    count = 0
    skipped = 0
    
    print(f"\n🌐 ПІДКЛЮЧЕННЯ ДО MIRO BOARD: {board_id}")
    print(f"🚀 РЕЖИМ: ЗАВАНТАЖЕННЯ МАКСИМАЛЬНОЇ ЯКОСТІ (HD)")
    print("-" * 60)
    
    try:
        while url:
            response = requests.get(url, headers=headers)
            if response.status_code != 200: 
                print(f"❌ Помилка API: {response.status_code}")
                break
            
            data = response.json()
            items = data.get('data', [])
            
            for item in items:
                item_id = item.get('id')
                item_data = item.get('data', {})
                temp_url_meta = item_data.get('imageUrl')
                
                if temp_url_meta:
                    # --- КЛЮЧОВИЙ ХАК №1: Заміна preview на original ---
                    high_res_url = temp_url_meta.replace("format=preview", "format=original")
                    
                    # Запит метаданих (Miro часто повертає JSON з посиланням на S3)
                    res_meta = requests.get(high_res_url, headers=headers)
                    
                    if res_meta.status_code == 200:
                        content_type = res_meta.headers.get('Content-Type', '')
                        
                        # --- КЛЮЧОВИЙ ХАК №2: Обробка JSON-редиректу ---
                        if "application/json" in content_type:
                            meta_json = res_meta.json()
                            # Пріоритет на originalUrl, потім на звичайний url
                            final_download_url = meta_json.get('originalUrl') or meta_json.get('url')
                        else:
                            # Якщо одразу прийшла картинка (рідко для HD)
                            final_download_url = high_res_url

                        if not final_download_url:
                            print(f"⚠️ Не вдалося знайти посилання для ID: {item_id}")
                            continue

                        # Формування імені файлу
                        name = item_data.get('title') or f"render_{item_id[-6:]}"
                        clean_name = re.sub(r'[\\/*?:"<>|]', "", str(name)).strip()[:40]
                        # Визначаємо розширення (можна було б додати перевірку байтів, але зазвичай PNG/JPG)
                        final_filename = f"{clean_name}_{item_id[-4:]}.png"
                        file_path = os.path.join(DATABASE_FOLDER, final_filename)

                        if not os.path.exists(file_path):
                            print(f"📥 HQ Завантаження: {final_filename}...", end=" ", flush=True)
                            
                            # Фінальне завантаження бінарних даних
                            img_res = requests.get(final_download_url)
                            if img_res.status_code == 200:
                                img_bytes = img_res.content
                                with open(file_path, 'wb') as f:
                                    f.write(img_bytes)
                                
                                size_kb = len(img_bytes) // 1024
                                print(f"✅ OK ({size_kb} KB)")
                                count += 1
                            else:
                                print(f"❌ Помилка завантаження файлу: {img_res.status_code}")
                        else:
                            print(f"⏩ Скіп (вже є): {final_filename}")
                            skipped += 1
                
            # Пагінація: перехід до наступної порції ітемів (якщо > 50)
            url = data.get('links', {}).get('next')
            
        print("-" * 60)
        print(f"📊 ПІДСУМОК: Нових HQ: {count} | Пропущено: {skipped}")
        return count, None

    except Exception as e:
        print(f"\n‼️ Критична помилка: {e}")
        import traceback
        traceback.print_exc() # Виведе повний шлях помилки в консоль
        return count, str(e)

def get_image_embedding(image):
    clip_emb = clip_model.encode(image, convert_to_tensor=True)
    inputs = dino_processor(images=image, return_tensors="pt")
    with torch.no_grad():
        outputs = dino_model(**inputs)
        dino_emb = outputs.last_hidden_state.mean(dim=1).flatten()
    combined = torch.cat((clip_emb, dino_emb))
    return combined / combined.norm(p=2)

def get_text_embedding(text):
    clip_text_emb = clip_model.encode(text, convert_to_tensor=True)
    padding = torch.zeros(768).to(clip_text_emb.device)
    combined = torch.cat((clip_text_emb, padding))
    return combined / combined.norm(p=2)

# --- 3. SIDEBAR ---
st.sidebar.title("🏛️ Freedes AI")

with st.sidebar.expander("☁️ Синхронізація з Miro"):
    miro_board = st.text_input("Board ID")
    miro_token = st.text_input("Token", type="password")
    if st.button("📥 Скачати оригінали"):
        c, err = get_miro_images(miro_board, miro_token)
        if err: st.error(err)
        else: st.success(f"Завантажено {c} фото")

# --- ОНОВЛЕНИЙ БЛОК ІНДЕКСАЦІЇ (З ГАРАНТОВАНИМ ВИВОДОМ У КОНСОЛЬ) ---
if st.sidebar.button("🔄 Оновити базу"):
    existing_data = []
    indexed_paths = set()
    
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, 'rb') as f:
            existing_data = pickle.load(f)
            indexed_paths = {item["full_path"] for item in existing_data}

    files_to_process = []
    log_area = st.sidebar.empty()
    
    # Вивід у консоль початку сканування
    print(f"\n{"="*40}")
    print(f"🔍 ПОШУК НОВИХ ФАЙЛІВ У: {DATABASE_FOLDER}")
    
    for root, dirs, files in os.walk(DATABASE_FOLDER):
        for f in files:
            full_path = os.path.normpath(os.path.join(root, f))
            actual_path = None
            
            if f.lower().endswith(('.jpg', '.jpeg', '.png', '.webp')):
                actual_path = full_path
            elif f.lower().endswith('.lnk'):
                target = get_shortcut_target(full_path)
                if target and os.path.exists(target):
                    actual_path = target if os.path.isfile(target) else None

            if actual_path and actual_path not in indexed_paths:
                files_to_process.append((actual_path, os.path.basename(actual_path)))

    if not files_to_process:
        print("✨ Нових зображень не виявлено. База актуальна.")
        print(f"{"="*40}\n")
        st.sidebar.info("Нових зображень не знайдено.")
    else:
        print(f"🚀 Знайдено нових об'єктів: {len(files_to_process)}")
        progress_bar = st.sidebar.progress(0)
        status_text = st.sidebar.empty()
        new_embeddings = []

        for i, (path, name) in enumerate(files_to_process):
            try:
                # ЦЕЙ РЯДОК ПИШЕ В КОНСОЛЬ (ЧОРНЕ ВІКНО)
                print(f"[{i+1}/{len(files_to_process)}] Обробка: {name}...", end=" ", flush=True)
                
                status_text.text(f"Аналіз ({i+1}/{len(files_to_process)}): {name}")
                img = Image.open(path).convert('RGB')
                
                # Ембединг головного фото
                emb = get_image_embedding(img)
                new_embeddings.append({"filename": name, "full_path": path, "embedding": emb})
                
                # YOLO об'єкти
                results = detector(img, verbose=False)
                for box in results[0].boxes:
                    x1, y1, x2, y2 = map(int, box.xyxy[0])
                    crop = img.crop((x1, y1, x2, y2))
                    crop_emb = get_image_embedding(crop)
                    new_embeddings.append({"filename": name, "full_path": path, "embedding": crop_emb})
                
                print("✅ Готово") # Дописує в рядок після успіху
                
            except Exception as e:
                print(f"❌ ПОМИЛКА: {e}")
                continue
            
            progress_bar.progress((i + 1) / len(files_to_process))
        
        final_db = existing_data + new_embeddings
        with open(CACHE_FILE, 'wb') as f:
            pickle.dump(final_db, f)
            
        print(f"💾 Базу успішно оновлено! Всього в кеші: {len(final_db)} записів.")
        print(f"{"="*40}\n")
        st.sidebar.success(f"Додано {len(files_to_process)} нових фото!")
        st.rerun()

# --- 4. ПОШУК ---
uploaded = st.sidebar.file_uploader("Завантажте фото", type=['jpg', 'png', 'jpeg'])
pasted = paste_image_button("📋 Вставити")
text_q = st.sidebar.text_input("Текст")
text_w = st.sidebar.slider("Вага тексту", 0, 100, 30) / 100

query_img = None
if uploaded: query_img = Image.open(uploaded).convert('RGB')
elif pasted.image_data: query_img = pasted.image_data.convert('RGB')

if os.path.exists(CACHE_FILE):
    with open(CACHE_FILE, 'rb') as f:
        db_data = pickle.load(f)
    
    if query_img:
        col1, col2 = st.columns([1, 1.2])
        with col1:
            cropped = st_cropper(query_img, realtime_update=True, box_color='#FF0000')
        with col2:
            img_emb = get_image_embedding(cropped)
            if text_q:
                t_emb = get_text_embedding(text_q)
                final_emb = (img_emb * (1-text_w)) + (t_emb * text_w)
                final_emb /= final_emb.norm(p=2)
            else: final_emb = img_emb

           # ... (попередній код отримання final_emb) ...

            embs = torch.stack([item["embedding"] for item in db_data])
            scores = util.cos_sim(final_emb, embs)[0]
            
            # Встановлюємо ліміт у 50 результатів (або менше, якщо в базі всього 10-20 фото)
            k_value = min(100, len(scores))
            tk = torch.topk(scores, k=k_value)
            
            # --- ОНОВЛЕНИЙ ВИВІД РЕЗУЛЬТАТІВ (БЕЗ ЖОРСТКОГО ЛІМІТУ) ---
            res_cols = st.columns(2)
            shown, count = set(), 0
            
            for s, idx in zip(tk.values, tk.indices):
                m = db_data[idx]
                f_path = m['full_path']
                f_name = m['filename']
                
                # Перевірка на дублікати (якщо один файл має кілька кропів від YOLO)
                if f_path not in shown:
                    if os.path.exists(f_path):
                        # Виводимо результат у колонки (ліва/права по черзі)
                        with res_cols[count % 2]:
                            # 1. Зображення
                            st.image(f_path, use_container_width=True)
                            
                            # 2. Назва файлу (чистимо від Miro-суфіксів)
                            display_name = re.sub(r'_[a-f0-9]{4}$', '', f_name.split('.')[0])
                            st.markdown(f"**{display_name}**")
                            
                            # 3. Відсоток схожості
                            st.write(f"🎯 Схожість: **{s:.1%}**")
                            st.divider()
                            
                        shown.add(f_path)
                        count += 1
                        
            # Якщо нічого не знайдено взагалі
            if count == 0:
                st.warning("Нічого не знайдено за вашим запитом.")import os
import re
import pickle
import requests
import torch
import json
import streamlit as st
from PIL import Image
import pythoncom
import win32com.client  # Для роботи з ярликами Windows

# Моделі та нейромережі
from ultralytics import YOLO
from sentence_transformers import SentenceTransformer, util
from transformers import AutoImageProcessor, AutoModel

# Компоненти інтерфейсу
from streamlit_cropper import st_cropper
from streamlit_paste_button import paste_image_button

# --- 1. КОНФІГУРАЦІЯ ---
os.environ["HF_TOKEN"] = "ВАШ ТОКЕН"
st.set_page_config(page_title="Freedes AI render search", layout="wide")

DATABASE_FOLDER = 'my_renders'
CACHE_FILE = 'embeddings_cache_ultra.pkl'
MODEL_CLIP = 'clip-ViT-L-14'
MODEL_DINO = 'facebook/dinov2-base'

if not os.path.exists(DATABASE_FOLDER):
    os.makedirs(DATABASE_FOLDER)

# --- 2. ДОПОМІЖНІ ФУНКЦІЇ ---

def get_shortcut_target(shortcut_path):
    """Отримує реальний шлях з Windows .lnk файлу"""
    pythoncom.CoInitialize() 
    try:
        shell = win32com.client.Dispatch("WScript.Shell")
        shortcut = shell.CreateShortCut(os.path.abspath(shortcut_path))
        return shortcut.TargetPath if shortcut.TargetPath else None
    except:
        return None

@st.cache_resource
def load_models():
    detector = YOLO('yolov8n.pt')
    clip_model = SentenceTransformer(MODEL_CLIP)
    dino_processor = AutoImageProcessor.from_pretrained(MODEL_DINO)
    dino_model = AutoModel.from_pretrained(MODEL_DINO)
    return detector, clip_model, dino_processor, dino_model

detector, clip_model, dino_processor, dino_model = load_models()

def get_miro_images(board_id, api_token):
    """Завантаження ОРИГІНАЛІВ (HD) з Miro з ПОВНИМ логуванням у консоль"""
    headers = {"Authorization": f"Bearer {api_token}", "Accept": "application/json"}
    # Початковий URL для отримання списку ітемів
    url = f"https://api.miro.com/v2/boards/{board_id}/items?type=image&limit=50"
    
    count = 0
    skipped = 0
    
    print(f"\n🌐 ПІДКЛЮЧЕННЯ ДО MIRO BOARD: {board_id}")
    print(f"🚀 РЕЖИМ: ЗАВАНТАЖЕННЯ МАКСИМАЛЬНОЇ ЯКОСТІ (HD)")
    print("-" * 60)
    
    try:
        while url:
            response = requests.get(url, headers=headers)
            if response.status_code != 200: 
                print(f"❌ Помилка API: {response.status_code}")
                break
            
            data = response.json()
            items = data.get('data', [])
            
            for item in items:
                item_id = item.get('id')
                item_data = item.get('data', {})
                temp_url_meta = item_data.get('imageUrl')
                
                if temp_url_meta:
                    # --- КЛЮЧОВИЙ ХАК №1: Заміна preview на original ---
                    high_res_url = temp_url_meta.replace("format=preview", "format=original")
                    
                    # Запит метаданих (Miro часто повертає JSON з посиланням на S3)
                    res_meta = requests.get(high_res_url, headers=headers)
                    
                    if res_meta.status_code == 200:
                        content_type = res_meta.headers.get('Content-Type', '')
                        
                        # --- КЛЮЧОВИЙ ХАК №2: Обробка JSON-редиректу ---
                        if "application/json" in content_type:
                            meta_json = res_meta.json()
                            # Пріоритет на originalUrl, потім на звичайний url
                            final_download_url = meta_json.get('originalUrl') or meta_json.get('url')
                        else:
                            # Якщо одразу прийшла картинка (рідко для HD)
                            final_download_url = high_res_url

                        if not final_download_url:
                            print(f"⚠️ Не вдалося знайти посилання для ID: {item_id}")
                            continue

                        # Формування імені файлу
                        name = item_data.get('title') or f"render_{item_id[-6:]}"
                        clean_name = re.sub(r'[\\/*?:"<>|]', "", str(name)).strip()[:40]
                        # Визначаємо розширення (можна було б додати перевірку байтів, але зазвичай PNG/JPG)
                        final_filename = f"{clean_name}_{item_id[-4:]}.png"
                        file_path = os.path.join(DATABASE_FOLDER, final_filename)

                        if not os.path.exists(file_path):
                            print(f"📥 HQ Завантаження: {final_filename}...", end=" ", flush=True)
                            
                            # Фінальне завантаження бінарних даних
                            img_res = requests.get(final_download_url)
                            if img_res.status_code == 200:
                                img_bytes = img_res.content
                                with open(file_path, 'wb') as f:
                                    f.write(img_bytes)
                                
                                size_kb = len(img_bytes) // 1024
                                print(f"✅ OK ({size_kb} KB)")
                                count += 1
                            else:
                                print(f"❌ Помилка завантаження файлу: {img_res.status_code}")
                        else:
                            print(f"⏩ Скіп (вже є): {final_filename}")
                            skipped += 1
                
            # Пагінація: перехід до наступної порції ітемів (якщо > 50)
            url = data.get('links', {}).get('next')
            
        print("-" * 60)
        print(f"📊 ПІДСУМОК: Нових HQ: {count} | Пропущено: {skipped}")
        return count, None

    except Exception as e:
        print(f"\n‼️ Критична помилка: {e}")
        import traceback
        traceback.print_exc() # Виведе повний шлях помилки в консоль
        return count, str(e)

def get_image_embedding(image):
    clip_emb = clip_model.encode(image, convert_to_tensor=True)
    inputs = dino_processor(images=image, return_tensors="pt")
    with torch.no_grad():
        outputs = dino_model(**inputs)
        dino_emb = outputs.last_hidden_state.mean(dim=1).flatten()
    combined = torch.cat((clip_emb, dino_emb))
    return combined / combined.norm(p=2)

def get_text_embedding(text):
    clip_text_emb = clip_model.encode(text, convert_to_tensor=True)
    padding = torch.zeros(768).to(clip_text_emb.device)
    combined = torch.cat((clip_text_emb, padding))
    return combined / combined.norm(p=2)

# --- 3. SIDEBAR ---
st.sidebar.title("🏛️ Freedes AI")

with st.sidebar.expander("☁️ Синхронізація з Miro"):
    miro_board = st.text_input("Board ID")
    miro_token = st.text_input("Token", type="password")
    if st.button("📥 Скачати оригінали"):
        c, err = get_miro_images(miro_board, miro_token)
        if err: st.error(err)
        else: st.success(f"Завантажено {c} фото")

# --- ОНОВЛЕНИЙ БЛОК ІНДЕКСАЦІЇ (INCREMENTAL UPDATE) ---
if st.sidebar.button("🔄 Оновити базу "):
    # 1. Завантажуємо існуючу базу, якщо вона є
    existing_data = []
    indexed_paths = set()
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, 'rb') as f:
            existing_data = pickle.load(f)
            # Створюємо набір шляхів, які ВЖЕ є в базі, для швидкої перевірки
            indexed_paths = {item["full_path"] for item in existing_data}

    files_to_process = []
    log_area = st.sidebar.empty()
    log_area.write("🔍 Пошук нових файлів...")
    
    # 2. Скануємо папку
    for root, dirs, files in os.walk(DATABASE_FOLDER):
        for f in files:
            full_path = os.path.normpath(os.path.join(root, f))
            
            # Обробка прямих файлів та ярликів
            actual_path = None
            if f.lower().endswith(('.jpg', '.jpeg', '.png', '.webp')):
                actual_path = full_path
            elif f.lower().endswith('.lnk'):
                target = get_shortcut_target(full_path)
                if target and os.path.exists(target):
                    if os.path.isfile(target): actual_path = target
                    # Для папок-ярликів логіка залишається (можна розширити за потреби)

            # ДОДАЄМО ТІЛЬКИ ЯКЩО ШЛЯХУ НЕМАЄ В indexed_paths
            if actual_path and actual_path not in indexed_paths:
                files_to_process.append((actual_path, os.path.basename(actual_path)))

    if not files_to_process:
        st.sidebar.info("✨ Нових зображень не знайдено. База актуальна!")
    else:
        progress_bar = st.sidebar.progress(0)
        status_text = st.sidebar.empty()
        
        total = len(files_to_process)
        new_embeddings = [] # Тимчасовий список для нових об'єктів

        for i, (path, name) in enumerate(files_to_process):
            try:
                status_text.text(f"Новий ({i+1}/{total}): {name}")
                img = Image.open(path).convert('RGB')
                
                # Головний ембединг
                emb = get_image_embedding(img)
                new_embeddings.append({"filename": name, "full_path": path, "embedding": emb})
                
                # YOLO кропи для нових фото
                results = detector(img, verbose=False)
                for box in results[0].boxes:
                    x1, y1, x2, y2 = map(int, box.xyxy[0])
                    crop = img.crop((x1, y1, x2, y2))
                    crop_emb = get_image_embedding(crop)
                    new_embeddings.append({"filename": name, "full_path": path, "embedding": crop_emb})
                
            except Exception as e:
                print(f"⚠️ Помилка файлу {path}: {e}")
                continue
            
            progress_bar.progress((i + 1) / total)
        
        # 3. Об'єднуємо старе з новим та зберігаємо
        final_db = existing_data + new_embeddings
        with open(CACHE_FILE, 'wb') as f:
            pickle.dump(final_db, f)
            
        st.sidebar.success(f"Додано {total} нових зображень!")
        st.rerun()

# --- 4. ПОШУК ---
uploaded = st.sidebar.file_uploader("Завантажте фото", type=['jpg', 'png', 'jpeg'])
pasted = paste_image_button("📋 Вставити")
text_q = st.sidebar.text_input("Текст")
text_w = st.sidebar.slider("Вага тексту", 0, 100, 30) / 100

query_img = None
if uploaded: query_img = Image.open(uploaded).convert('RGB')
elif pasted.image_data: query_img = pasted.image_data.convert('RGB')

if os.path.exists(CACHE_FILE):
    with open(CACHE_FILE, 'rb') as f:
        db_data = pickle.load(f)
    
    if query_img:
        col1, col2 = st.columns([1, 1.2])
        with col1:
            cropped = st_cropper(query_img, realtime_update=True, box_color='#FF0000')
        with col2:
            img_emb = get_image_embedding(cropped)
            if text_q:
                t_emb = get_text_embedding(text_q)
                final_emb = (img_emb * (1-text_w)) + (t_emb * text_w)
                final_emb /= final_emb.norm(p=2)
            else: final_emb = img_emb

           # ... (попередній код отримання final_emb) ...

            embs = torch.stack([item["embedding"] for item in db_data])
            scores = util.cos_sim(final_emb, embs)[0]
            
            # Встановлюємо ліміт у 50 результатів (або менше, якщо в базі всього 10-20 фото)
            k_value = min(100, len(scores))
            tk = torch.topk(scores, k=k_value)
            
            # --- ОНОВЛЕНИЙ ВИВІД РЕЗУЛЬТАТІВ (БЕЗ ЖОРСТКОГО ЛІМІТУ) ---
            res_cols = st.columns(2)
            shown, count = set(), 0
            
            for s, idx in zip(tk.values, tk.indices):
                m = db_data[idx]
                f_path = m['full_path']
                f_name = m['filename']
                
                # Перевірка на дублікати (якщо один файл має кілька кропів від YOLO)
                if f_path not in shown:
                    if os.path.exists(f_path):
                        # Виводимо результат у колонки (ліва/права по черзі)
                        with res_cols[count % 2]:
                            # 1. Зображення
                            st.image(f_path, use_container_width=True)
                            
                            # 2. Назва файлу (чистимо від Miro-суфіксів)
                            display_name = re.sub(r'_[a-f0-9]{4}$', '', f_name.split('.')[0])
                            st.markdown(f"**{display_name}**")
                            
                            # 3. Відсоток схожості
                            st.write(f"🎯 Схожість: **{s:.1%}**")
                            st.divider()
                            
                        shown.add(f_path)
                        count += 1
                        
            # Якщо нічого не знайдено взагалі
            if count == 0:
                st.warning("Нічого не знайдено за вашим запитом.")
