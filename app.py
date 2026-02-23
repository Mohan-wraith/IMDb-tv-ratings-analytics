import streamlit as st
import sqlite3
import pandas as pd
import numpy as np
import requests
import os, math, textwrap, re, time, json
from PIL import Image, ImageDraw, ImageFont
from io import BytesIO
from bs4 import BeautifulSoup
from fake_useragent import UserAgent

# --- CONFIG ---
DB_FILE = "tv_shows.db"

# ==========================================
# 🎨 CUSTOM CSS (THE "PRO" LOOK)
# ==========================================
st.set_page_config(layout="wide", page_title="TV Heatmap", page_icon="📺")

st.markdown("""
<style>
    /* Make the app full width and remove padding */
    .block-container {
        padding-top: 2rem;
        padding-bottom: 2rem;
        max-width: 95% !important;
    }
    
    /* Hide standard Streamlit header */
    header {visibility: hidden;}
    
    /* Stylish Title */
    .main-title {
        font-size: 3.5rem;
        font-weight: 800;
        background: -webkit-linear-gradient(45deg, #FF4B2B, #FF416C);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        margin-bottom: 0.5rem;
    }
    
    /* Card-like background for show details */
    .show-card {
        background-color: #1E1E1E;
        padding: 20px;
        border-radius: 15px;
        box-shadow: 0 4px 15px rgba(0,0,0,0.3);
        margin-bottom: 20px;
    }
    
    /* Recommendation Text */
    .rec-title {
        font-size: 1.1rem;
        font-weight: 600;
        margin-top: 5px;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
    }
</style>
""", unsafe_allow_html=True)

# ==========================================
# 🎨 VISUALIZATION ENGINE
# ==========================================

def strip_html(s):
    if not s: return ""
    return re.sub(r'<[^>]*>', '', s).strip()

def draw_star(draw, center, size, color):
    cx, cy = center
    pts = []
    inner = size * 0.45
    outer = size
    ang = -math.pi / 2
    for i in range(10):
        r = outer if i % 2 == 0 else inner
        pts.append((cx + math.cos(ang) * r, cy + math.sin(ang) * r))
        ang += math.pi / 5
    draw.polygon(pts, fill=color)

# --- UPDATED COLOR LOGIC ---
def color_for_score(score):
    if score is None or (isinstance(score, float) and np.isnan(score)): return (54, 54, 54)
    s = float(score)
    # 1. Garbage remains <= 5.5
    if s <= 5.5: return (128, 0, 128) 
    # 2. Removed the distinct "Bad" red category (<= 6.9)
    # 3. "Good" now catches everything from 5.6 up to 7.9
    if s <= 7.9: return (212, 175, 55) 
    if s <= 8.5: return (144, 238, 144)
    return (0, 100, 0)
# ---------------------------

def text_color_for_bg(rgb):
    r, g, b = rgb
    return (0, 0, 0) if (0.299 * r + 0.587 * g + 0.114 * b) > 150 else (255, 255, 255)

def cover_crop(img, W, H):
    if img is None: return None
    sw, sh = img.size
    scale = max(W / sw, H / sh)
    nw, nh = int(sw * scale), int(sh * scale)
    img2 = img.resize((nw, nh), Image.LANCZOS)
    left = max(0, (nw - W) // 2)
    top = max(0, (nh - H) // 2)
    return img2.crop((left, top, left + W, top + H))

def draw_text_rich(draw, pos, text, font, fill, shadow=(0, 0, 0, 200), shadow_offset=(2, 2), anchor=None):
    x, y = pos
    sx, sy = shadow_offset
    if shadow:
        draw.text((x + sx, y + sy), text, font=font, fill=shadow, anchor=anchor)
    draw.text((x, y), text, font=font, fill=fill, anchor=anchor)

def load_font(name_list, size):
    for name in name_list:
        try:
            return ImageFont.truetype(name, size)
        except:
            continue
    return ImageFont.load_default()

def wrap_text_pixel(draw, text, font, max_w):
    words = text.split()
    lines = []
    current_line = []
    for word in words:
        test_line = ' '.join(current_line + [word])
        w = draw.textlength(test_line, font=font)
        if w <= max_w:
            current_line.append(word)
        else:
            if current_line:
                lines.append(' '.join(current_line))
                current_line = [word]
            else:
                lines.append(word)
                current_line = []
    if current_line:
        lines.append(' '.join(current_line))
    return lines

# --- BRIGHT 3D GOLDEN BORDER FUNCTION ---
def draw_golden_3d_border(draw, bbox, border_width=4):
    """Draws a bright 3D beveled golden border around a specific episode block."""
    x0, y0, x1, y1 = bbox
    
    # 3D Bevel Colors (The Brighter Version)
    bright_gold = "#FFD700"  # Top & Left (Highlight)
    dark_gold = "#B8860B"    # Bottom & Right (Shadow)
    inner_glow = "#FFFACD"   # 1px inner pop
    
    for i in range(border_width):
        draw.line([(x0+i, y0+i), (x1-i, y0+i)], fill=bright_gold, width=1) # Top
        draw.line([(x0+i, y0+i), (x0+i, y1-i)], fill=bright_gold, width=1) # Left
        draw.line([(x0+i, y1-i), (x1-i, y1-i)], fill=dark_gold, width=1)   # Bottom
        draw.line([(x1-i, y0+i), (x1-i, y1-i)], fill=dark_gold, width=1)   # Right

    # 1px inner pop to make it crisp
    inner_offset = border_width
    draw.rectangle([x0 + inner_offset, y0 + inner_offset, x1 - inner_offset, y1 - inner_offset], outline=inner_glow, width=1)

def render_page(grid_df, poster_img, title, year_range, summary, main_rating, num_votes=0):
    left_col_w = 600
    HEADER_Y = 90
    FIXED_BOX_W = 110
    FIXED_BOX_H = 42
    GAP_BETWEEN_COLS = 80
    
    num_seasons = len(grid_df.columns)
    grid_width = num_seasons * (FIXED_BOX_W + 12)
    required_width = 60 + left_col_w + GAP_BETWEEN_COLS + grid_width + 100
    canvas_w = max(1920, required_width) 
    
    n_eps = grid_df.shape[0]
    base_top = 350
    spacing = FIXED_BOX_H + 12
    min_required_h = base_top + n_eps * spacing + 300
    canvas_h = max(1080, min_required_h)

    canvas = Image.new("RGB", (canvas_w, canvas_h), (0, 0, 0))
    draw = ImageDraw.Draw(canvas)

    # --- FONT DEFINITIONS ---
    f_reg = load_font(["arial.ttf", "LiberationSans-Regular.ttf", "DejaVuSans.ttf"], 20)
    title_font = load_font(["arialbd.ttf", "LiberationSans-Bold.ttf", "DejaVuSans-Bold.ttf", "arial.ttf"], 72)
    font_year = load_font(["arial.ttf", "LiberationSans-Regular.ttf", "DejaVuSans.ttf"], 28)
    font_rating = load_font(["arialbd.ttf", "LiberationSans-Bold.ttf", "DejaVuSans-Bold.ttf", "arial.ttf"], 56)
    font_votes = load_font(["arial.ttf", "LiberationSans-Regular.ttf", "DejaVuSans.ttf"], 28) 
    box_font = load_font(["arialbd.ttf", "LiberationSans-Bold.ttf", "DejaVuSans-Bold.ttf", "arial.ttf"], 22)

    if poster_img:
        bg = cover_crop(poster_img, canvas_w, canvas_h)
        if bg:
            canvas.paste(bg, (0, 0))
            overlay = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 150))
            canvas.paste(overlay, (0, 0), overlay)

    x_left = 60
    y = 40
    draw_text_rich(draw, (x_left, y), "TV Series", f_reg, (245, 245, 245))
    y += 28
    title_lines = wrap_text_pixel(draw, title, title_font, max_w=550)
    for line in title_lines:
        draw_text_rich(draw, (x_left, y), line, title_font, (245, 245, 245))
        y += 75 
    y += 10
    draw_text_rich(draw, (x_left, y), f"({year_range})", font_year, (245, 245, 245))
    y += 70
    
    # --- DRAW STAR, RATING, AND VOTES ---
    draw_star(draw, (x_left + 28, y + 25), 28, (255, 200, 0))
    rating_text = f"{main_rating}/10"
    draw_text_rich(draw, (x_left + 70, y), rating_text, font_rating, (245, 245, 245))
    
    if pd.notna(num_votes) and num_votes != 0 and num_votes != "":
        try:
            votes_str = f"({int(num_votes):,})" 
            rating_w = draw.textlength(rating_text, font=font_rating)
            draw_text_rich(draw, (x_left + 70 + rating_w + 15, y + 22), votes_str, font_votes, (180, 180, 180))
        except:
            pass
            
    y += 75
    wrapper = textwrap.TextWrapper(width=50)
    lines = wrapper.wrap(summary)[:8]
    for line in lines:
        draw_text_rich(draw, (x_left, y), line, f_reg, (210, 210, 210))
        y += 30

    grid_start_x = x_left + left_col_w + GAP_BETWEEN_COLS
    seasons = list(grid_df.columns)
    
    # --- UPDATED LEGEND (Removed "Bad" category) ---
    legend = [("Awesome", (0, 100, 0)), ("Great", (144, 238, 144)), ("Good", (212, 175, 55)), ("Garbage", (128, 0, 128))]
    # -----------------------------------------------
    
    lx = grid_start_x
    ly = HEADER_Y - 60
    for name, col in legend:
        draw.ellipse((lx, ly, lx+12, ly+12), fill=col)
        draw_text_rich(draw, (lx+20, ly+6), name, f_reg, (245,245,245), anchor="lm")
        text_w = draw.textlength(name, font=f_reg)
        lx += (20 + text_w + 40)

    for j, s in enumerate(seasons):
        sx = grid_start_x + j * (FIXED_BOX_W + 12)
        box = [sx, HEADER_Y - 16, sx + FIXED_BOX_W, HEADER_Y + 16]
        draw.rounded_rectangle(box, radius=10, fill=(40, 40, 40))
        draw_text_rich(draw, (sx + FIXED_BOX_W / 2, HEADER_Y), f"S{s}", f_reg, (245, 245, 245), anchor="mm")

    row_top = HEADER_Y + 40 
    for i, ep in enumerate(grid_df.index):
        ry = row_top + i * (FIXED_BOX_H + 12)
        ebox = [grid_start_x - 66, ry, grid_start_x - 66 + 48, ry + FIXED_BOX_H]
        draw.rounded_rectangle(ebox, 6, (40, 40, 40))
        draw_text_rich(draw, (ebox[0] + 24, ebox[1] + FIXED_BOX_H / 2), f"E{ep}", f_reg, (245, 245, 245), anchor="mm")
        
        for j, s in enumerate(seasons):
            sx = grid_start_x + j * (FIXED_BOX_W + 12)
            val = grid_df.loc[ep, s]
            box = [sx, ry, sx + FIXED_BOX_W, ry + FIXED_BOX_H]
            
            fill = color_for_score(val)
            draw.rounded_rectangle(box, radius=10, fill=fill, outline=(12, 12, 12), width=2)
            
            if pd.notna(val) and val >= 9.5:
                border_box = [box[0]+1, box[1]+1, box[2]-1, box[3]-1]
                draw_golden_3d_border(draw, border_box)
            
            txt = f"{val:.1f}" if pd.notna(val) else "-"
            tcol = text_color_for_bg(fill)
            draw_text_rich(draw, (sx + FIXED_BOX_W / 2, ry + FIXED_BOX_H / 2), txt, box_font, tcol, anchor="mm", shadow=None)

    sep = row_top + n_eps * (FIXED_BOX_H + 12) + 8
    draw.line([(grid_start_x - 80, sep), (grid_start_x + len(seasons) * (FIXED_BOX_W + 12), sep)], fill=(60, 60, 60), width=2)
    avg_y = sep + 12
    a = [grid_start_x - 66, avg_y, grid_start_x - 66 + 48, avg_y + FIXED_BOX_H]
    draw.rounded_rectangle(a, 8, (40, 40, 40))
    draw_text_rich(draw, (a[0] + 24, avg_y + FIXED_BOX_H / 2), "Avg", f_reg, (245, 245, 245), anchor="mm")
    
    for j, s in enumerate(seasons):
        sx = grid_start_x + j * (FIXED_BOX_W + 12)
        vals = pd.to_numeric(grid_df[s], errors='coerce').dropna()
        avg = round(vals.mean(), 1) if len(vals) > 0 else None
        b = [sx, avg_y, sx + 110, avg_y + FIXED_BOX_H]
        fill = color_for_score(avg)
        draw.rounded_rectangle(b, FIXED_BOX_H // 2, fill)
        txt = f"{avg:.1f}" if avg is not None else "—"
        tcol = text_color_for_bg(fill)
        draw_text_rich(draw, (sx + 55, avg_y + FIXED_BOX_H / 2), txt, box_font, tcol, anchor="mm", shadow=None)
    
    return canvas

# ==========================================
# 🧠 BACKEND LOGIC
# ==========================================

def get_recommendations(current_tconst, genres):
    if not genres or genres == "Unknown": return pd.DataFrame()
    current_genres = genres.split(',')
    main_genre = current_genres[0]
    conn = sqlite3.connect(DB_FILE)
    score_query = []
    for g in current_genres:
        score_query.append(f"(CASE WHEN s.genres LIKE '%{g}%' THEN 1 ELSE 0 END)")
    total_score_sql = " + ".join(score_query)
    sql = f"""
    SELECT s.tconst, s.primaryTitle, s.startYear, s.numVotes, s.genres, r.averageRating,
           ({total_score_sql}) as match_score
    FROM shows s
    JOIN ratings r ON s.tconst = r.tconst
    WHERE s.genres LIKE ? AND s.tconst != ?
    ORDER BY match_score DESC, s.numVotes DESC
    LIMIT 4
    """
    params = [f"%{main_genre}%", current_tconst]
    try:
        df = pd.read_sql_query(sql, conn, params=params)
    except:
        df = pd.DataFrame()
    conn.close()
    return df

def search_shows(query):
    conn = sqlite3.connect(DB_FILE)
    parts = query.split()
    sql = "SELECT tconst, primaryTitle, startYear, numVotes, genres FROM shows WHERE "
    conditions = []
    params = []
    for part in parts:
        conditions.append("primaryTitle LIKE ?")
        params.append(f"%{part}%")
    sql += " AND ".join(conditions)
    sql += " ORDER BY numVotes DESC LIMIT 15"
    try:
        df = pd.read_sql_query(sql, conn, params=params)
    except:
        df = pd.DataFrame()
    conn.close()
    return df

def scrape_live_ratings(imdb_id):
    data = []
    ua = UserAgent()
    headers = {"User-Agent": ua.chrome}
    session = requests.Session()
    for s in range(1, 40):
        if len(data) > 0 and s > data[-1]['seasonNumber'] + 2: break
        try:
            url = f"https://www.imdb.com/title/{imdb_id}/episodes?season={s}"
            r = session.get(url, headers=headers, timeout=8)
            if r.status_code != 200: continue
            soup = BeautifulSoup(r.content, 'html.parser')
            found = False
            json_tag = soup.find('script', id='__NEXT_DATA__')
            if json_tag:
                try:
                    js = json.loads(json_tag.string)
                    ep_items = js['props']['pageProps']['contentData']['section']['episodes']['items']
                    for item in ep_items:
                        ep = int(item['episode'])
                        val = float(item['rating']['aggregateRating'])
                        if ep > 0 and val > 0:
                            data.append({'seasonNumber': s, 'episodeNumber': ep, 'averageRating': val})
                            found = True
                except: pass
            if not found:
                stars = soup.select('.ipc-rating-star--rating')
                for i, star in enumerate(stars):
                    try:
                        val = float(star.text.strip())
                        if val > 0:
                            data.append({'seasonNumber': s, 'episodeNumber': i+1, 'averageRating': val})
                            found = True
                    except: pass
        except: pass
    return pd.DataFrame(data).drop_duplicates(subset=['seasonNumber', 'episodeNumber'])

def get_live_overall_rating(tconst):
    try:
        ua = UserAgent()
        headers = {"User-Agent": ua.chrome}
        url = f"https://www.imdb.com/title/{tconst}/"
        r = requests.get(url, headers=headers, timeout=5)
        soup = BeautifulSoup(r.content, 'html.parser')
        json_tag = soup.find('script', type='application/ld+json')
        if json_tag:
            data = json.loads(json_tag.string)
            return float(data['aggregateRating']['ratingValue'])
    except: return None
    return None

def get_show_data(tconst, force_live=False):
    conn = sqlite3.connect(DB_FILE)
    source_msg = "Database"
    q_main = "SELECT averageRating FROM ratings WHERE tconst = ?"
    main_rating_df = pd.read_sql_query(q_main, conn, params=(tconst,))
    if not main_rating_df.empty and pd.notna(main_rating_df.iloc[0]['averageRating']):
        main_rating = round(main_rating_df.iloc[0]['averageRating'], 1)
    else: main_rating = 0.0
    df = pd.DataFrame()
    if force_live:
        try:
            df = scrape_live_ratings(tconst)
            if not df.empty: source_msg = "Live IMDb (Fresh)"
            live_main = get_live_overall_rating(tconst)
            if live_main: main_rating = live_main
        except: pass
    if df.empty:
        q_eps = "SELECT e.seasonNumber, e.episodeNumber, r.averageRating FROM episodes e JOIN ratings r ON e.tconst = r.tconst WHERE e.parentTconst = ? ORDER BY e.seasonNumber, e.episodeNumber"
        df = pd.read_sql_query(q_eps, conn, params=(tconst,))
        if force_live: source_msg = "Live Failed (Using DB)"
    conn.close()
    if df.empty: return None, "No episode data found.", source_msg
    df = df[df['seasonNumber'] > 0]
    df = df.drop_duplicates(subset=['seasonNumber', 'episodeNumber'], keep='last')
    grid = df.pivot(index="episodeNumber", columns="seasonNumber", values="averageRating")
    if main_rating == 0.0 and not df.empty: main_rating = round(df['averageRating'].mean(), 1)
    return grid, main_rating, source_msg

def get_metadata(imdb_id, quality="medium"):
    poster_url = None
    summary = ""
    try:
        url = f"https://api.tvmaze.com/lookup/shows?imdb={imdb_id}"
        r = requests.get(url, timeout=2) 
        if r.status_code == 200:
            d = r.json()
            poster_url = d.get("image", {}).get(quality) 
            summary = strip_html(d.get("summary", ""))
    except: pass
    return poster_url, summary

# ==========================================
# 🚀 PRO INTERFACE
# ==========================================

st.markdown('<p class="main-title">🔥 TV HEATMAP</p>', unsafe_allow_html=True)
if not os.path.exists(DB_FILE):
    st.error("⚠️ Database missing! Please run 'build_db.py' first.")
    st.stop()

query = st.text_input("", placeholder="🔍 Search for a show (e.g. Arcane, Breaking Bad)...")

if query:
    results = search_shows(query)
    if results.empty:
        st.warning(f"No shows found for '{query}'.")
    else:
        st.markdown("### Select Show:")
        cols = st.columns(3)
        for i, (idx, row) in enumerate(results.iterrows()):
            with cols[i % 3]:
                label = f"{row['primaryTitle']} ({row['startYear']})"
                if st.button(label, key=f"btn_{row['tconst']}", use_container_width=True):
                    st.session_state['selected_show'] = row.to_dict()
                    st.rerun()

main_display = st.empty()

if 'selected_show' in st.session_state:
    with main_display.container():
        row = st.session_state['selected_show']
        target_id = row['tconst']
        
        st.divider()
        
        poster_url, summary = get_metadata(target_id, quality="original")
        
        hero_col1, hero_col2 = st.columns([1, 2])
        
        with hero_col1:
            if poster_url:
                st.image(poster_url, use_container_width=True)
            else:
                st.markdown("📺 No Poster")
            
        with hero_col2:
            st.markdown(f"# {row['primaryTitle']}")
            genres = row.get('genres', 'Unknown')
            start_year = row.get('startYear', '????')
            st.markdown(f"#### {start_year} • {genres}")
            
            c1, c2 = st.columns(2)
            do_db = c1.button("⚡ Fast (DB)", key="act_db", use_container_width=True)
            do_live = c2.button("🌍 Live (Web)", key="act_live", use_container_width=True)
            
            use_live = False
            if do_live: use_live = True
            
            if summary:
                st.markdown(f"_{summary}_")

        if do_db or do_live or 'selected_show' in st.session_state:
            with st.spinner("Generating Heatmap..."):
                grid, rating, src_msg = get_show_data(target_id, force_live=use_live)
                if grid is not None:
                    if "Live" in src_msg: st.success(f"✅ Data Source: {src_msg}")
                    else: st.caption(f"ℹ️ Data Source: {src_msg}")
                    
                    poster_img = None
                    if poster_url:
                        try:
                            resp = requests.get(poster_url)
                            poster_img = Image.open(BytesIO(resp.content)).convert("RGB")
                        except: pass
                    
                    final_img = render_page(
                        grid, 
                        poster_img, 
                        row['primaryTitle'], 
                        row.get('startYear', '????'), 
                        summary, 
                        rating,
                        row.get('numVotes', 0)
                    )
                    st.image(final_img, use_container_width=True)
                    
                    buf = BytesIO()
                    final_img.save(buf, format="PNG")
                    st.download_button("⬇️ Download High-Res Image", data=buf.getvalue(), file_name=f"{row['primaryTitle']}.png", mime="image/png", use_container_width=True)
                    
                    st.divider()
                    st.subheader("You might also like:")
                    
                    current_genres = row.get('genres', 'Unknown')
                    rec_df = get_recommendations(target_id, current_genres)
                    
                    if not rec_df.empty:
                        rcols = st.columns(4)
                        for idx, rec_row in rec_df.iterrows():
                            with rcols[idx]:
                                rec_poster, _ = get_metadata(rec_row['tconst'], quality="medium")
                                if rec_poster: st.image(rec_poster, use_container_width=True)
                                else: st.markdown("📺 *No Poster*")
                                
                                st.caption(f"⭐ {rec_row['averageRating']}")
                                
                                if st.button(f"▶ {rec_row['primaryTitle']}", key=f"rec_{rec_row['tconst']}", use_container_width=True):
                                    st.session_state['selected_show'] = rec_row.to_dict()
                                    st.rerun()
