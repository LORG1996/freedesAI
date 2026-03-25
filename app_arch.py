import os
import re
import pickle
import requests
import torch
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
    """Завантаження ОРИГІНАЛІВ з Miro з ПОВНИМ логуванням у консоль"""
    headers = {"Authorization": f"Bearer {api_token}", "Accept": "application/json"}
    url = f"https://api.miro.com/v2/boards/{board_id}/items?type=image&limit=50"
    count = 0
    skipped = 0
    
    print(f"\n🌐 ПІДКЛЮЧЕННЯ ДО MIRO BOARD: {board_id}")
    print("-" * 50)
    
    try:
        while url:
            response = requests.get(url, headers=headers)
            if response.status_code != 200: 
                print(f"❌ Помилка API: {response.status_code}")
                break
            
            data = response.json()
            for item in data.get('data', []):
                item_id, item_data = item.get('id'), item.get('data', {})
                temp_url_meta = item_data.get('imageUrl')
                
                if temp_url_meta:
                    res_meta = requests.get(temp_url_meta, headers=headers)
                    if res_meta.status_code == 200:
                        final_url = res_meta.json().get('url')
                        name = item_data.get('title') or f"render_{item_id[-4:]}"
                        clean_name = re.sub(r'[\\/*?:"<>|]', "", str(name))[:40]
                        final_filename = f"{clean_name}_{item_id[-4:]}.jpg"
                        file_path = os.path.join(DATABASE_FOLDER, final_filename)

                        if not os.path.exists(file_path):
                            print(f"📥 Завантаження: {final_filename}...", end=" ", flush=True)
                            img_data = requests.get(final_url).content
                            with open(file_path, 'wb') as f:
                                f.write(img_data)
                            print("✅ OK")
                            count += 1
                        else:
                            print(f"⏩ Скіп (вже є): {final_filename}")
                            skipped += 1
            url = data.get('links', {}).get('next')
        print("-" * 50)
        print(f"📊 ПІДСУМОК: Нових: {count} | Пропущено: {skipped}")
        return count, None
    except Exception as e:
        print(f"‼️ Критична помилка: {e}")
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

# --- ОНОВЛЕНИЙ БЛОК ІНДЕКСАЦІЇ З ЛОГУВАННЯМ ---
if st.sidebar.button("🔄 Оновити базу (Індексація ШІ)"):
    files_to_process = []
    log_area = st.sidebar.empty() # Місце для тексту над прогрес-баром
    log_area.write("🔍 Сканування папок та ярликів...")
    
    # 1. Етап збору файлів
    for root, dirs, files in os.walk(DATABASE_FOLDER):
        for f in files:
            full_path = os.path.normpath(os.path.join(root, f))
            
            # Прямі зображення
            if f.lower().endswith(('.jpg', '.jpeg', '.png', '.webp')):
                files_to_process.append((full_path, f))
                
            # Обробка ярликів
            elif f.lower().endswith('.lnk'):
                target = get_shortcut_target(full_path)
                if target and os.path.exists(target):
                    print(f"🔗 Ярлик {f} -> {target}") # Лог у консоль
                    if os.path.isdir(target):
                        for s_root, _, s_files in os.walk(target):
                            for sf in s_files:
                                if sf.lower().endswith(('.jpg', '.jpeg', '.png', '.webp')):
                                    files_to_process.append((os.path.join(s_root, sf), sf))
                    elif target.lower().endswith(('.jpg', '.jpeg', '.png', '.webp')):
                        files_to_process.append((target, os.path.basename(target)))

    if not files_to_process:
        st.sidebar.error("Зображень не знайдено!")
        print(f"❌ Помилка: Папка {DATABASE_FOLDER} порожня.")
    else:
        db_data = []
        progress_bar = st.sidebar.progress(0)
        status_text = st.sidebar.empty() # Текст для назви файлу в UI
        
        total = len(files_to_process)
        print(f"\n🚀 Початок індексації: {total} об'єктів")
        print("-" * 50)

        for i, (path, name) in enumerate(files_to_process):
            try:
                # Вивід у веб-інтерфейс
                status_text.text(f"Обробка ({i+1}/{total}): {name}")
                # Вивід у консоль
                print(f"[{i+1}/{total}] Аналіз: {path}")
                
                img = Image.open(path).convert('RGB')
                
                # Головний ембединг
                emb = get_image_embedding(img)
                db_data.append({"filename": name, "full_path": path, "embedding": emb})
                
                # Пошук об'єктів через YOLO та створення кропів
                results = detector(img, verbose=False)
                for box in results[0].boxes:
                    x1, y1, x2, y2 = map(int, box.xyxy[0])
                    crop = img.crop((x1, y1, x2, y2))
                    crop_emb = get_image_embedding(crop)
                    db_data.append({"filename": name, "full_path": path, "embedding": crop_emb})
                
            except Exception as e:
                print(f"⚠️ Помилка файлу {path}: {e}")
                continue
            
            # Оновлення прогресу
            progress_bar.progress((i + 1) / total)
        
        # Збереження
        with open(CACHE_FILE, 'wb') as f:
            pickle.dump(db_data, f)
            
        print("-" * 50)
        print(f"✅ Готово! Базу оновлено. Файл: {CACHE_FILE}")
        st.sidebar.success(f"Успіх! Оброблено {total} зображень.")
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

            embs = torch.stack([item["embedding"] for item in db_data])
            scores = util.cos_sim(final_emb, embs)[0]
            tk = torch.topk(scores, k=min(20, len(scores)))
            
            # --- ОНОВЛЕНИЙ ВИВІД РЕЗУЛЬТАТІВ ---
            res_cols = st.columns(2)
            shown, count = set(), 0
            
            for s, idx in zip(tk.values, tk.indices):
                m = db_data[idx]
                f_path = m['full_path']
                f_name = m['filename']
                
                if f_path not in shown and count < 10:
                    if os.path.exists(f_path):
                        with res_cols[count % 2]:
                            # 1. Зображення
                            st.image(f_path, use_container_width=True)
                            
                            # 2. НАЗВА (зверху, жирним)
                            display_name = re.sub(r'_[a-f0-9]{4}$', '', f_name.split('.')[0])
                            st.markdown(f"**{display_name}**")
                            
                            # 3. ВІДСОТКИ (під назвою)
                            st.write(f"🎯 Схожість: **{s:.1%}**")
                            
                            st.divider()
                            
                        shown.add(f_path)
                        count += 1
