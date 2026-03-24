import streamlit as st
import os
import pickle
import torch
from PIL import Image
from streamlit_cropper import st_cropper
from ultralytics import YOLO
from sentence_transformers import SentenceTransformer, util
from transformers import AutoImageProcessor, AutoModel
# Додаємо нову бібліотеку
from streamlit_paste_button import paste_image_button

# --- КОНФІГУРАЦІЯ ---
# (Тут твій токен та налаштування залишаються без змін)
os.environ["HF_TOKEN"] = "твій токен"

st.set_page_config(page_title="Freedes AI render search", layout="wide")
DATABASE_FOLDER = 'my_renders'
CACHE_FILE = 'embeddings_cache_ultra.pkl'
MODEL_CLIP = 'clip-ViT-L-14'
MODEL_DINO = 'facebook/dinov2-base'

if not os.path.exists(DATABASE_FOLDER):
    os.makedirs(DATABASE_FOLDER)

# --- ЗАВАНТАЖЕННЯ МОДЕЛЕЙ ---
@st.cache_resource
def load_models():
    hf_token = os.environ.get("HF_TOKEN")
    detector = YOLO('yolov8n.pt')
    clip_model = SentenceTransformer(MODEL_CLIP)
    dino_processor = AutoImageProcessor.from_pretrained(MODEL_DINO, token=hf_token)
    dino_model = AutoModel.from_pretrained(MODEL_DINO, token=hf_token)
    return detector, clip_model, dino_processor, dino_model

detector, clip_model, dino_processor, dino_model = load_models()

# --- ФУНКЦІЇ ЕМБЕДИНГІВ ---
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

# --- SIDEBAR ---
st.sidebar.title("🏛️ Freedes AI")
st.sidebar.subheader("📂 База даних")

if st.sidebar.button("🔄 Оновити базу (Індексація)"):
    # (Тут твоя логіка індексації залишається без змін)
    files = [f for f in os.listdir(DATABASE_FOLDER) if f.lower().endswith(('.jpg', '.jpeg', '.png', '.webp'))]
    if not files:
        st.sidebar.error("Папка 'my_renders' порожня!")
    else:
        db_data = []
        progress = st.progress(0)
        for i, filename in enumerate(files):
            img_path = os.path.join(DATABASE_FOLDER, filename)
            try:
                img = Image.open(img_path).convert('RGB')
                db_data.append({"filename": filename, "embedding": get_image_embedding(img)})
                results = detector(img, verbose=False)
                for box in results[0].boxes:
                    x1, y1, x2, y2 = map(int, box.xyxy[0])
                    crop = img.crop((x1, y1, x2, y2))
                    db_data.append({"filename": filename, "embedding": get_image_embedding(crop)})
            except: continue
            progress.progress((i + 1) / len(files))
        with open(CACHE_FILE, 'wb') as f:
            pickle.dump(db_data, f)
        st.sidebar.success(f"Готово!")
        st.rerun()

# --- ПАРАМЕТРИ ПОШУКУ ---
st.sidebar.markdown("---")
st.sidebar.subheader("🔍 Введення даних")

# 1. Вибір файлу
uploaded_file = st.sidebar.file_uploader("Завантажте файл", type=['jpg', 'png', 'jpeg'])

# 2. Кнопка вставки з буфера (CTRL+V)
st.sidebar.write("АБО")
pasted_image = paste_image_button(
    label="📋 Вставити з буфера (CTRL+V)",
    background_color="#FF4B4B",
    hover_background_color="#D33636",
    errors="ignore"
)

text_query = st.sidebar.text_input("Текстовий фільтр", "")
text_weight = st.sidebar.slider("Вага тексту (%)", 0, 100, 30) / 100

# --- ЛОГІКА ВИБОРУ ЗОБРАЖЕННЯ ---
query_img = None
if uploaded_file:
    query_img = Image.open(uploaded_file).convert('RGB')
elif pasted_image.image_data is not None:
    query_img = pasted_image.image_data.convert('RGB')

# --- ОСНОВНА ЧАСТИНА ---
if not os.path.exists(CACHE_FILE):
    st.warning("Будь ласка, натисніть 'Оновити базу' в меню зліва.")
    st.stop()

with open(CACHE_FILE, 'rb') as f:
    db_data = pickle.load(f)

if query_img:
    col1, col2 = st.columns([1, 1.2])

    with col1:
        st.subheader("1. Кроп-фільтр")
        cropped_img = st_cropper(query_img, realtime_update=True, box_color='#FF0000', aspect_ratio=None)
        st.image(cropped_img, caption="Область пошуку", width=300)

    with col2:
        st.subheader("2. Результати")
        img_emb = get_image_embedding(cropped_img)
        
        if text_query:
            txt_emb = get_text_embedding(text_query)
            final_emb = (img_emb * (1 - text_weight)) + (txt_emb * text_weight)
            final_emb = final_emb / final_emb.norm(p=2)
        else:
            final_emb = img_emb

        all_embs = torch.stack([item["embedding"] for item in db_data])
        scores = util.cos_sim(final_emb, all_embs)[0]
        top_k = torch.topk(scores, k=min(10, len(scores)))

        res_cols = st.columns(2)
        shown = set()
        count = 0
        for score, idx in zip(top_k.values, top_k.indices):
            match = db_data[idx]
            if match['filename'] not in shown and count < 6:
                with res_cols[count % 2]:
                    st.image(os.path.join(DATABASE_FOLDER, match['filename']), use_container_width=True)
                    st.write(f"**Схожість: {score:.1%}**")
                    st.caption(f"ID: {match['filename']}")
                shown.add(match['filename'])
                count += 1
else:
    st.info("💡 Завантажте фото або натисніть кнопку вставки з буфера.")
