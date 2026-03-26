import os
import re
import pickle
import requests
import torch
import json
import streamlit as st
from PIL import Image
import pythoncom
import win32com.client

# Моделі
from ultralytics import YOLO
from sentence_transformers import SentenceTransformer, util
from transformers import AutoImageProcessor, AutoModel

# UI компоненти
from streamlit_cropper import st_cropper
from streamlit_paste_button import paste_image_button

# --- 1. КОНФІГУРАЦІЯ ---
os.environ["HF_TOKEN"] = "ВАШ ТОКЕН"
st.set_page_config(page_title="Freedes AI Search", layout="wide")

DATABASE_FOLDER = 'my_renders'
MIRO_SUBFOLDER = os.path.join(DATABASE_FOLDER, 'miro')
CACHE_FILE = 'embeddings_cache_ultra.pkl'
MIRO_MAP_FILE = 'miro_mapping.json'
MODEL_CLIP = 'clip-ViT-L-14'
MODEL_DINO = 'facebook/dinov2-base'

# Створення папок
for folder in [DATABASE_FOLDER, MIRO_SUBFOLDER]:
    if not os.path.exists(folder):
        os.makedirs(folder)

# --- 2. ДОПОМІЖНІ ФУНКЦІЇ ---

def get_shortcut_target(shortcut_path):
    """Отримує реальний шлях з Windows .lnk файлу"""
    pythoncom.CoInitialize() 
    try:
        shell = win32com.client.Dispatch("WScript.Shell")
        abs_path = os.path.abspath(shortcut_path)
        shortcut = shell.CreateShortCut(abs_path)
        target = shortcut.TargetPath
        return os.path.realpath(target) if target and os.path.exists(target) else None
    except:
        return None

def load_miro_map():
    if os.path.exists(MIRO_MAP_FILE):
        with open(MIRO_MAP_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}

def save_miro_map(m_map):
    with open(MIRO_MAP_FILE, 'w', encoding='utf-8') as f:
        json.dump(m_map, f, ensure_ascii=False, indent=4)

@st.cache_resource
def load_models():
    detector = YOLO('yolov8n.pt')
    clip_model = SentenceTransformer(MODEL_CLIP)
    dino_processor = AutoImageProcessor.from_pretrained(MODEL_DINO)
    dino_model = AutoModel.from_pretrained(MODEL_DINO)
    return detector, clip_model, dino_processor, dino_model

detector, clip_model, dino_processor, dino_model = load_models()

def get_miro_images(board_id, api_token):
    """Завантаження зображень з Miro з миттєвим пропуском уже відомих ID"""
    headers = {"Authorization": f"Bearer {api_token}", "Accept": "application/json"}
    url = f"https://api.miro.com/v2/boards/{board_id}/items?type=image&limit=50"
    
    miro_map = load_miro_map()
    # Створюємо набір вже існуючих ID для блискавичної перевірки
    existing_ids = {info['id'] for info in miro_map.values()}
    
    count, skipped = 0, 0
    print(f"\n{'='*60}\n🌐 ШВИДКА СИНХРОНІЗАЦІЯ MIRO: {board_id}\n{'='*60}", flush=True)
    
    try:
        while url:
            res = requests.get(url, headers=headers)
            if res.status_code != 200: break
            data = res.json()
            
            for item in data.get('data', []):
                item_id = item.get('id')
                
                # --- ГОЛОВНИЙ ФІКС ДЛЯ ШВИДКОСТІ ---
                if item_id in existing_ids:
                    skipped += 1
                    continue # Пропускаємо об'єкт ВІДРАЗУ, без запитів до мережі
                # ----------------------------------

                img_url_meta = item.get('data', {}).get('imageUrl')
                if img_url_meta:
                    hq_url = img_url_meta.replace("format=preview", "format=original")
                    res_meta = requests.get(hq_url, headers=headers)
                    f_url = None
                    if res_meta.status_code == 200:
                        content_type = res_meta.headers.get('Content-Type','')
                        if "application/json" in content_type:
                            f_url = res_meta.json().get('originalUrl') or res_meta.json().get('url')
                        else:
                            f_url = hq_url

                    if not f_url or f_url == 'None':
                        continue

                    name = item.get('data', {}).get('title') or f"render_{item_id[-6:]}"
                    clean_n = re.sub(r'[\\/*?:"<>|]', "", str(name)).strip()[:40]
                    f_name = f"{clean_n}_{item_id[-4:]}.png"
                    f_path = os.path.join(MIRO_SUBFOLDER, f_name)
                    
                    # Подвійна перевірка (по ID та по файлу)
                    if not os.path.exists(f_path):
                        print(f"📥 Новий рендер: {f_name}...", end=" ", flush=True)
                        img_res = requests.get(f_url)
                        if img_res.status_code == 200:
                            with open(f_path, 'wb') as f: f.write(img_res.content)
                            miro_map[f_name] = {"id": item_id, "board": board_id}
                            existing_ids.add(item_id) # Додаємо в список, щоб не дублювати
                            count += 1
                            print("✅", flush=True)
                    else:
                        skipped += 1
            
            url = data.get('links', {}).get('next')
            
        save_miro_map(miro_map)
        print(f"\n📊 ГОТОВО: Додано {count}, Пропущено (вже є) {skipped}", flush=True)
        return count, None
    except Exception as e:
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

# ПОВЕРНЕНО: Вікно синхронізації Miro
with st.sidebar.expander("☁️ Miro Cloud Sync", expanded=False):
    m_id = st.text_input("Board ID", placeholder="uXjVP...")
    m_tk = st.text_input("Token", type="password")
    if st.button("📥 Скачати нові рендери"):
        if m_id and m_tk:
            with st.spinner("Завантаження з Miro..."):
                c, err = get_miro_images(m_id, m_tk)
                if err: st.error(f"Помилка: {err}")
                else: 
                    st.success(f"Додано {c} зображень")
                    st.rerun()
        else:
            st.warning("Введіть ID та Token")

# --- ОНОВЛЕНИЙ БЛОК ІНДЕКСАЦІЇ З ПОВІДОМЛЕННЯМ ПРО ПЕРЕЗАПИС ---
if st.sidebar.button("🔄 Оновити базу (Scan & Index)"):
    existing_data = []
    indexed_paths = set()
    
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, 'rb') as f:
            existing_data = pickle.load(f)
            # Створюємо набір нормалізованих шляхів для перевірки
            indexed_paths = {os.path.normcase(os.path.realpath(item["full_path"])) for item in existing_data}

    files_to_process = []
    log_area = st.sidebar.empty()
    log_area.write("🔍 Сканування нових файлів...")
    
    print(f"\n{'='*60}\n🔍 ІНКРЕМЕНТАЛЬНЕ СКАНУВАННЯ\n{'='*60}", flush=True)

    # Сканування
    for root, _, files in os.walk(DATABASE_FOLDER):
        for f in files:
            full_path = os.path.join(root, f)
            targets = []

            if f.lower().endswith(('.jpg', '.jpeg', '.png', '.webp')):
                targets.append(os.path.realpath(full_path))
            elif f.lower().endswith('.lnk'):
                print(f"🔗 Ярлик: {f}", end=" ", flush=True)
                t = get_shortcut_target(full_path)
                if t:
                    if os.path.isfile(t):
                        print("-> Файл ✅", flush=True)
                        targets.append(t)
                    elif os.path.isdir(t):
                        print("-> ПАПКА 📂", flush=True)
                        for s_root, _, s_files in os.walk(t):
                            for s_f in s_files:
                                if s_f.lower().endswith(('.jpg', '.jpeg', '.png', '.webp')):
                                    targets.append(os.path.realpath(os.path.join(s_root, s_f)))
                else:
                    print("-> ❌ Битий", flush=True)

            # Відбір тільки унікальних нових
            for target_path in targets:
                norm_target = os.path.normcase(target_path)
                if norm_target not in indexed_paths:
                    files_to_process.append((target_path, os.path.basename(target_path)))
                    indexed_paths.add(norm_target) 

    if files_to_process:
        new_embeddings = []
        progress_bar = st.sidebar.progress(0)
        status_text = st.sidebar.empty()
        
        # Обробка нових
        for i, (path, name) in enumerate(files_to_process):
            try:
                print(f"[{i+1}/{len(files_to_process)}] Аналіз: {name}...", end=" ", flush=True)
                status_text.text(f"Обробка ({i+1}/{len(files_to_process)}): {name}")
                
                img = Image.open(path).convert('RGB')
                
                # Головний ембединг
                emb = get_image_embedding(img)
                new_embeddings.append({"filename": name, "full_path": path, "embedding": emb})
                
                # YOLO ембединги кропів
                results = detector(img, verbose=False)
                for box in results[0].boxes:
                    x1, y1, x2, y2 = map(int, box.xyxy[0])
                    crop = img.crop((x1, y1, x2, y2))
                    crop_emb = get_image_embedding(crop)
                    new_embeddings.append({"filename": name, "full_path": path, "embedding": crop_emb})
                
                print("✅ Готово", flush=True)
                
            except Exception as e:
                print(f"❌ Помилка ({e})", flush=True)
                continue
            
            progress_bar.progress((i + 1) / len(files_to_process))
        
        # Об'єднання
        # Ми створюємо новий список, щоб гарантувати чистоту даних
        final_db_list = existing_data + new_embeddings
        
        # --- НОВЕ ПОВІДОМЛЕННЯ В КОНСОЛЬ ---
        print(f"\n💾 ЗБЕРЕЖЕННЯ: Перезапис файлу кешу {CACHE_FILE}...", end=" ", flush=True)
        print(f"(Додано {len(new_embeddings)} записів, всього {len(final_db_list)})...", end=" ", flush=True)
        
        try:
            # Фізичне збереження (перезапис)
            with open(CACHE_FILE, 'wb') as f:
                pickle.dump(final_db_list, f)
            print("✅ УСПІШНО!", flush=True)
            print(f"{'='*60}\n", flush=True)
            st.sidebar.success(f"Базу оновлено! Додано {len(files_to_process)} нових фото."); st.rerun()
        except Exception as save_error:
            print(f"❌ ПОМИЛКА ЗБЕРЕЖЕННЯ: {save_error}", flush=True)
            print(f"{'='*60}\n", flush=True)
            st.sidebar.error(f"Не вдалося зберегти базу: {save_error}")

    else:
        print("✨ Нових зображень не знайдено.", flush=True)
        print(f"{'='*60}\n", flush=True)
        st.sidebar.info("Нових зображень не знайдено.")

        
# --- 4. ПОШУК ТА 60 РЕЗУЛЬТАТІВ ---
uploaded = st.sidebar.file_uploader("Пошук", type=['jpg', 'png', 'jpeg'])
pasted = paste_image_button("📋 Вставити")
text_q = st.sidebar.text_input("📝 Опис")
text_w = st.sidebar.slider("Вага тексту", 0, 100, 30) / 100

if os.path.exists(CACHE_FILE):
    with open(CACHE_FILE, 'rb') as f: db_data = pickle.load(f)
    m_map = load_miro_map()

    query_img = None
    if uploaded: query_img = Image.open(uploaded).convert('RGB')
    elif pasted.image_data: query_img = pasted.image_data.convert('RGB')

    if query_img or text_q:
        if query_img:
            c1, c2 = st.columns([1, 1.2])
            with c1: cropped = st_cropper(query_img, realtime_update=True)
            img_emb = get_image_embedding(cropped)
        else: img_emb = torch.zeros(1536)

        if text_q:
            t_emb = get_text_embedding(text_q)
            final_emb = (img_emb * (1-text_w)) + (t_emb * text_w) if query_img else t_emb
            final_emb /= final_emb.norm(p=2)
        else: final_emb = img_emb

        scores = util.cos_sim(final_emb, torch.stack([item["embedding"] for item in db_data]))[0]
        
        # ТУТ МИ ВИВОДИМО 60 НАЙКРАЩИХ РЕЗУЛЬТАТІВ (можна змінити на 75)
        tk = torch.topk(scores, k=min(60, len(scores)))

        res_cols = st.columns(2)
        shown, count = set(), 0
        for s, idx in zip(tk.values, tk.indices):
            m = db_data[idx]; p, n = m['full_path'], m['filename']
            if p not in shown and os.path.exists(p):
                with res_cols[count % 2]:
                    st.image(p, use_container_width=True)
                    if n in m_map:
                        info = m_map[n]
                        st.link_button("🔗 Miro", f"https://miro.com/app/board/{info['board']}/?moveToWidget={info['id']}")
                    st.caption(f"🎯 {s:.1%} | {n}")
                    st.divider()
                shown.add(p); count += 1
