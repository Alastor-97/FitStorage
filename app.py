import streamlit as st
import fitparse
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import os
import datetime
import io
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

# Configurazione della pagina
st.set_page_config(page_title="Coach Dashboard Pro", layout="wide")

# --- LOGIN SEMPLICE ---
def require_login():
    if "authenticated" not in st.session_state:
        st.session_state.authenticated = False
    if "login_error" not in st.session_state:
        st.session_state.login_error = ""

    if not st.session_state.authenticated:
        # Funzione riutilizzabile per login via Enter o bottone
        def do_login():
            user_val = st.session_state.get("login_user", "")
            pwd_val = st.session_state.get("login_pwd", "")
            if user_val == "root" and pwd_val == "Fit2026!":
                st.session_state.authenticated = True
                st.session_state.login_error = ""
            else:
                st.session_state.login_error = "Credenziali non valide."

        # Centriamo il form di login
        col_left, col_center, col_right = st.columns([1, 2, 1])
        with col_center:
            st.markdown("### 🔐 Login")
            user = st.text_input("Utente", key="login_user")
            pwd = st.text_input(
                "Password",
                type="password",
                key="login_pwd",
                on_change=do_login  # Invio da tastiera sulla password prova il login
            )
            if st.button("Accedi"):
                do_login()

            if st.session_state.login_error:
                st.error(st.session_state.login_error)
            elif st.session_state.authenticated:
                st.success("Accesso riuscito. Benvenuto!")
        st.stop()

require_login()

# Titolo mostrato solo dopo login
st.title("🚴‍♂️ Dashboard Performance & Progressi")

# --- CONFIGURAZIONE ---
# Folder ID può essere configurato nei secrets o hardcoded qui
if 'config' in st.secrets and 'google_drive_folder_id' in st.secrets['config']:
    GOOGLE_DRIVE_FOLDER_ID = st.secrets['config']['google_drive_folder_id']
else:
    GOOGLE_DRIVE_FOLDER_ID = "1b-nerBbVjtzxDJnVIeMuVRfg4vlRmrji"
SCOPES = ['https://www.googleapis.com/auth/drive.readonly']

# --- FUNZIONI GOOGLE DRIVE ---
@st.cache_resource
def get_drive_service():
    """Autentica e restituisce il servizio Google Drive usando Service Account da Streamlit secrets o file locale."""
    try:
        # Prova prima a caricare dai secrets di Streamlit (produzione/cloud)
        if 'google_credentials' in st.secrets:
            creds_dict = st.secrets['google_credentials']
            creds = service_account.Credentials.from_service_account_info(
                creds_dict,
                scopes=SCOPES
            )
        # Fallback: prova a caricare da file locale (sviluppo)
        elif os.path.exists('credentials.json'):
            creds = service_account.Credentials.from_service_account_file(
                'credentials.json',
                scopes=SCOPES
            )
        else:
            st.error("Credenziali Google non trovate.")
            st.info("""
            Configura le credenziali in uno di questi modi:
            
            **Per Streamlit Cloud:**
            1. Vai su https://share.streamlit.io
            2. Seleziona la tua app
            3. Vai su Settings > Secrets
            4. Aggiungi la sezione 'google_credentials' con il contenuto del tuo credentials.json
            
            **Per sviluppo locale:**
            - Metti il file credentials.json nella directory del progetto
            """)
            st.stop()
        
        return build('drive', 'v3', credentials=creds)
    except Exception as e:
        st.error(f"Errore durante l'autenticazione con Service Account: {e}")
        st.info("Verifica che le credenziali siano corrette e che il Service Account abbia i permessi necessari su Google Drive")
        st.stop()

@st.cache_data(ttl=300)  # Cache per 5 minuti
def list_drive_files(folder_id):
    """Ottiene la lista di file .fit dalla cartella Google Drive."""
    try:
        service = get_drive_service()
        query = f"'{folder_id}' in parents and mimeType != 'application/vnd.google-apps.folder' and name contains '.fit'"
        results = service.files().list(q=query, fields="files(id, name)").execute()
        files = results.get('files', [])
        # Restituisce lista di tuple (nome_file, file_id)
        return [(f['name'], f['id']) for f in files]
    except Exception as e:
        st.error(f"Errore nel recupero file da Google Drive: {e}")
        return []

def download_file_from_drive(file_id):
    """Scarica un file da Google Drive e restituisce i dati binari."""
    try:
        service = get_drive_service()
        request = service.files().get_media(fileId=file_id)
        file_data = io.BytesIO()
        downloader = MediaIoBaseDownload(file_data, request)
        done = False
        while done is False:
            status, done = downloader.next_chunk()
        file_data.seek(0)
        return file_data
    except Exception as e:
        st.error(f"Errore nel download del file: {e}")
        return None

# --- FUNZIONI DI CARICAMENTO E CALCOLO ---

def calculate_ftp_estimate(df):
    """Calcola l'FTP stimato come il 95% della miglior potenza media di 20 minuti."""
    if 'power' in df.columns:
        # Assumendo campionamento a 1Hz, 20 minuti = 1200 record
        window = 1200 
        if len(df) >= window:
            mmp20 = df['power'].rolling(window=window).mean().max()
            return int(mmp20 * 0.95)
        else:
            # Se l'attività è più corta di 20 min, facciamo una stima prudenziale sulla potenza media
            return int(df['power'].mean())
    return 250


def calculate_ftp_from_last_n_activities(all_files_dict, n):
    """
    Calcola l'FTP stimato analizzando le ultime N attività disponibili,
    concatenando i dati di potenza e riutilizzando la logica di calculate_ftp_estimate.
    all_files_dict: dict con chiave=nome_file, valore=file_id
    """
    if not all_files_dict:
        return 250

    # Prendiamo le ultime N attività in ordine alfabetico (tipicamente i file hanno data nel nome)
    sorted_files = sorted(all_files_dict.items())[-n:]

    dfs = []
    for fname, file_id in sorted_files:
        df_temp = load_single_fit_from_drive(file_id)
        if not df_temp.empty and 'power' in df_temp.columns:
            dfs.append(df_temp)

    if not dfs:
        return 250

    df_all = pd.concat(dfs, ignore_index=True)
    return calculate_ftp_estimate(df_all)

def load_single_fit(file_data):
    """Carica i dati completi di un singolo file da dati binari."""
    try:
        fitfile = fitparse.FitFile(file_data)
        data = []
        for record in fitfile.get_messages("record"):
            r = {field.name: field.value for field in record}
            data.append(r)
        
        df = pd.DataFrame(data)
        
        if 'timestamp' in df.columns:
            df['timestamp'] = pd.to_datetime(df['timestamp'])
            start = df['timestamp'].iloc[0]
            df['minuti_trascorsi'] = (df['timestamp'] - start).dt.total_seconds() / 60
        
        if 'speed' in df.columns: df['speed_kmh'] = df['speed'] * 3.6
        if 'enhanced_altitude' in df.columns: df['altitude_m'] = df['enhanced_altitude']
        elif 'altitude' in df.columns: df['altitude_m'] = df['altitude']
            
        return df
    except Exception as e:
        return pd.DataFrame()

@st.cache_data
def load_single_fit_from_drive(file_id):
    """Scarica e carica un file FIT da Google Drive."""
    file_data = download_file_from_drive(file_id)
    if file_data:
        return load_single_fit(file_data)
    return pd.DataFrame()

@st.cache_data
def get_activity_summary(files_dict):
    """
    Legge velocemente tutti i file per i trend da Google Drive.
    files_dict: dict con chiave=nome_file, valore=file_id
    """
    summary_data = []
    progress_bar = st.progress(0)
    total_files = len(files_dict)
    
    for i, (filename, file_id) in enumerate(files_dict.items()):
        try:
            file_data = download_file_from_drive(file_id)
            if file_data:
                fitfile = fitparse.FitFile(file_data)
                records = [{field.name: field.value for field in record} for record in fitfile.get_messages("record")]
                df_temp = pd.DataFrame(records)
                
                if not df_temp.empty and 'timestamp' in df_temp.columns:
                    date = pd.to_datetime(df_temp['timestamp'].iloc[0])
                    dist = df_temp['distance'].max() / 1000 if 'distance' in df_temp.columns else 0
                    speed_avg = (df_temp['speed'].mean() * 3.6) if 'speed' in df_temp.columns else 0
                    power_avg = df_temp['power'].mean() if 'power' in df_temp.columns else 0
                    cad_avg = df_temp[df_temp['cadence'] > 0]['cadence'].mean() if 'cadence' in df_temp.columns else 0
                    hr_avg = df_temp['heart_rate'].mean() if 'heart_rate' in df_temp.columns else 0
                    
                    ele_gain = 0
                    if 'enhanced_altitude' in df_temp.columns:
                        ele_gain = df_temp['enhanced_altitude'].max() - df_temp['enhanced_altitude'].min()
                    
                    duration_min = (df_temp['timestamp'].iloc[-1] - df_temp['timestamp'].iloc[0]).total_seconds() / 60

                    summary_data.append({
                        'Filename': filename, 'Data': date, 'Distanza (km)': round(dist, 2),
                        'Velocità Avg (km/h)': round(speed_avg, 1), 'Potenza Avg (W)': int(power_avg),
                        'Cadenza Avg (rpm)': int(cad_avg), 'FC Avg (bpm)': int(hr_avg),
                        'Dislivello (m)': int(ele_gain), 'Durata (min)': int(duration_min)
                    })
        except Exception as e:
            st.error(f"Errore durante la lettura del file '{filename}': {e}")
        progress_bar.progress((i + 1) / total_files)
    progress_bar.empty()
    return pd.DataFrame(summary_data).sort_values(by='Data')

# --- LOGICA APPLICAZIONE ---

# Ottieni lista file da Google Drive
drive_files = list_drive_files(GOOGLE_DRIVE_FOLDER_ID)

if not drive_files:
    st.warning("Nessun file .fit trovato nella cartella Google Drive.")
    st.stop()

# Crea dizionario nome_file -> file_id e ordina per nome (decrescente per date AAAAMMGG)
files_dict = {name: file_id for name, file_id in drive_files}
all_files = sorted(files_dict.keys(), reverse=True)

with st.sidebar:
    st.header("🧭 Navigazione")
    app_mode = st.radio("Seleziona Modalità:", ["📊 Analisi Singola Attività", "📈 Analisi Trend & Progressi"])
    st.markdown("---")

# ==============================================================================
# MODALITÀ 1: ANALISI SINGOLA
# ==============================================================================
if app_mode == "📊 Analisi Singola Attività":
    
    with st.sidebar:
        st.subheader("Impostazioni Analisi")
        file_selezionato = st.selectbox("Scegli attività:", all_files)
        file_id = files_dict[file_selezionato]
        
        # Carichiamo i dati della singola attività
        df = load_single_fit_from_drive(file_id)
        
        st.markdown("---")
        st.write("🔧 **Configurazione Atleta**")
        
        # Peso atleta (usato per stima calorie)
        user_weight = st.number_input(
            "Peso (kg)",
            min_value=30,
            max_value=150,
            value=60,
            step=1,
            help="Peso corporeo utilizzato per stimare il consumo calorico."
        )
        
        # Calcolo stima dinamica FTP sulle ultime 5 attività (non solo su questa)
        ftp_stimato = calculate_ftp_from_last_n_activities(files_dict, 5)
        
        # Input FTP con valore di default stimato (ultime 5 attività)
        user_ftp = st.number_input(
            "Il tuo FTP (Watt):",
            min_value=50,
            max_value=600,
            value=ftp_stimato,
            step=5,
            help=f"FTP stimato sulle ultime 5 attività: {ftp_stimato}W (≈95% della miglior potenza media di 20 minuti complessiva). Puoi modificarlo se conosci il tuo valore reale."
        )

        # Rapporto peso/potenza legato all'FTP (mostrato in configurazione atleta)
        wkg_ftp_sidebar = user_ftp / user_weight if user_weight > 0 else 0
        st.text_input(
            "Rapporto peso/potenza FTP (W/kg)",
            value=f"{wkg_ftp_sidebar:.2f}",
            help="Calcolato come FTP / peso corporeo. Indica quanti Watt sviluppi per kg di peso: utile per confrontare le prestazioni tra atleti diversi.",
            disabled=True,
        )

    if not df.empty:
        st.markdown(f"## 🔎 Dettaglio: {file_selezionato}")
        
        # Calcoli base per KPI
        dist_km = df['distance'].max() / 1000 if 'distance' in df.columns else 0
        durata_min = (df['timestamp'].iloc[-1] - df['timestamp'].iloc[0]).total_seconds() / 60 if 'timestamp' in df.columns else 0
        speed_avg = df['speed_kmh'].mean() if 'speed_kmh' in df.columns else 0
        p_avg = df['power'].mean() if 'power' in df.columns else 0
        hr_avg = df['heart_rate'].mean() if 'heart_rate' in df.columns else 0
        cad_avg = df[df['cadence'] > 0]['cadence'].mean() if 'cadence' in df.columns else 0
        gain = df['altitude_m'].max() - df['altitude_m'].min() if 'altitude_m' in df.columns else 0

        # Consumo calorico stimato (usato anche nei KPI)
        kcal = 0
        if dist_km > 0:
            kcal = 0.3 * user_weight * dist_km

        # Rapporti peso/potenza
        wkg_ftp = user_ftp / user_weight if user_weight > 0 else 0
        wkg_session = p_avg / user_weight if user_weight > 0 else 0

        # KPI - Prima riga
        r1c1, r1c2, r1c3, r1c4, r1c5, r1c6 = st.columns(6)
        r1c1.metric("Distanza", f"{dist_km:.2f} km")
        r1c2.metric("Durata", f"{durata_min:.0f} min")
        r1c3.metric("Velocità Avg", f"{speed_avg:.1f} km/h")
        r1c4.metric("Potenza Avg", f"{int(p_avg)} W")
        r1c5.metric(
            "FTP Stimato",
            f"{ftp_stimato} W",
            help="FTP stimato sulle ultime 5 attività (≈95% della miglior potenza media di 20 minuti complessiva)."
        )
        r1c6.metric(
            "FTP W/kg",
            f"{wkg_ftp:.2f} W/kg",
            help="Rapporto peso/potenza basato sull'FTP (FTP / peso). Utile per confrontare le prestazioni tra atleti di peso diverso."
        )

        # KPI - Seconda riga
        r2c1, r2c2, r2c3, r2c4, r2c5, r2c6 = st.columns(6)
        r2c1.metric("FC Media", f"{hr_avg:.0f} bpm")
        r2c2.metric("Cadenza Avg", f"{int(cad_avg)} rpm")
        r2c3.metric("Dislivello", f"{int(gain)} m")
        r2c4.metric("Consumo stimato", f"{kcal:.0f} kcal")
        r2c5.metric("W/kg Sessione", f"{wkg_session:.2f} W/kg")
        r2c6.metric("Peso", f"{user_weight:.1f} kg")

        x_axis = 'distance' if 'distance' in df.columns else 'minuti_trascorsi'
        x_label = 'Distanza (metri)' if 'distance' in df.columns else 'Minuti'
        
        st.markdown("---")

        # --- GRAFICO TUTTO IN UNO ---
        st.subheader("📊 Confronto Tutto in Uno")
        chk1, chk2, chk3, chk4, chk5, chk_norm = st.columns(6)
        show_speed = chk1.checkbox("Velocità", value=True) if 'speed_kmh' in df.columns else False
        show_power = chk2.checkbox("Potenza", value=True) if 'power' in df.columns else False
        show_cadence = chk3.checkbox("Cadenza", value=False) if 'cadence' in df.columns else False
        show_altitude = chk4.checkbox("Altitudine", value=False) if 'altitude_m' in df.columns else False
        show_hr = chk5.checkbox("Freq. Cardiaca", value=False) if 'heart_rate' in df.columns else False
        normalize = chk_norm.checkbox("Normalizza %", value=True)

        selected_cols = []
        if show_speed: selected_cols.append('speed_kmh')
        if show_power: selected_cols.append('power')
        if show_cadence: selected_cols.append('cadence')
        if show_altitude: selected_cols.append('altitude_m')
        if show_hr: selected_cols.append('heart_rate')

        if selected_cols:
            plot_df = df[[x_axis] + selected_cols].copy()
            if normalize:
                for col in selected_cols:
                    mx, mn = plot_df[col].max(), plot_df[col].min()
                    if mx > mn: plot_df[col] = (plot_df[col] - mn) / (mx - mn) * 100
            fig_comp = px.line(plot_df, x=x_axis, y=selected_cols)
            fig_comp.update_layout(xaxis_title=x_label, template="plotly_white", hovermode="x unified")
            st.plotly_chart(fig_comp, use_container_width=True)

        st.markdown("---")

        # --- ALTIMETRIA ---
        if 'altitude_m' in df.columns:
            alt_max, alt_avg = df['altitude_m'].max(), df['altitude_m'].mean()

            # Calcolo pendenze se disponibile la distanza
            avg_grade, max_grade = None, None
            if 'distance' in df.columns and df['distance'].max() > 0:
                total_dist_m = df['distance'].max()
                gain = alt_max - df['altitude_m'].min()
                avg_grade = (gain / total_dist_m) * 100

                # Serie di pendenze punto-punto per hover
                dist_diff = df['distance'].diff()
                alt_diff = df['altitude_m'].diff()
                mask = dist_diff > 0
                grades = (alt_diff[mask] / dist_diff[mask]) * 100
                # Salviamo nel dataframe per usarla nel tooltip
                df['grade_pct'] = 0.0
                df.loc[mask, 'grade_pct'] = grades
                if not grades.empty:
                    max_grade = grades.max()

            title_extra = ""
            if avg_grade is not None and max_grade is not None:
                title_extra = f" | Pend. media: {avg_grade:.1f}% | Pend. max: {max_grade:.1f}%"

            st.subheader(
                f"⛰️ Profilo Altimetrico (Max: {int(alt_max)}m | Avg: {int(alt_avg)}m{title_extra})"
            )

            # Calcolo consumo calorico stimato (dipende da peso e distanza)
            if 'distance' in df.columns:
                dist_km = df['distance'].max() / 1000
                # Stima semplice: ~0.3 kcal per kg per km
                kcal = 0.3 * user_weight * dist_km
                c1, c2, c3 = st.columns(3)
                if avg_grade is not None:
                    c1.metric("Pendenza media", f"{avg_grade:.1f} %")
                if max_grade is not None:
                    c2.metric("Pendenza max", f"{max_grade:.1f} %")
                c3.metric("Consumo stimato", f"{kcal:.0f} kcal")

            min_y = min(0, df['altitude_m'].min())
            fig_alt = go.Figure()
            fig_alt.add_trace(
                go.Scatter(
                    x=df[x_axis],
                    y=[min_y] * len(df),
                    mode='lines',
                    line=dict(width=0),
                    showlegend=False,
                    hoverinfo='skip'
                )
            )

            # Tooltip con pendenza se disponibile
            if 'grade_pct' in df.columns:
                customdata = df['grade_pct']
                hovertemplate = (
                    "Altitudine: %{y:.0f} m<br>"
                    "Pendenza: %{customdata:.1f} %<extra></extra>"
                )
                fig_alt.add_trace(
                    go.Scatter(
                        x=df[x_axis],
                        y=df['altitude_m'],
                        fill='tonexty',
                        line=dict(color='#555555'),
                        fillcolor='rgba(85, 85, 85, 0.5)',
                        customdata=customdata,
                        hovertemplate=hovertemplate,
                    )
                )
            else:
                fig_alt.add_trace(
                    go.Scatter(
                        x=df[x_axis],
                        y=df['altitude_m'],
                        fill='tonexty',
                        line=dict(color='#555555'),
                        fillcolor='rgba(85, 85, 85, 0.5)',
                    )
                )
            fig_alt.update_layout(xaxis_title=x_label, yaxis_title="m", template="plotly_white", showlegend=False)
            st.plotly_chart(fig_alt, use_container_width=True)

        # --- POTENZA E ZONE ---
        if 'power' in df.columns:
            p_max, p_avg = df['power'].max(), df['power'].mean()
            col_p1, col_p2 = st.columns([2, 1])
            with col_p1:
                st.subheader(f"⚡ Potenza (Max: {int(p_max)}W | Avg: {int(p_avg)}W)")
                df['p_smooth'] = df['power'].rolling(10).mean()
                fig_pwr = px.area(df, x=x_axis, y='p_smooth', color_discrete_sequence=['#FFA500'])
                fig_pwr.update_traces(fillcolor='rgba(255, 165, 0, 0.3)', line=dict(width=1))
                fig_pwr.update_layout(xaxis_title=x_label, yaxis_title="Watt", template="plotly_white")
                st.plotly_chart(fig_pwr, use_container_width=True)
            with col_p2:
                st.subheader(f"📊 Zone (FTP: {user_ftp}W)")
                bins = [-1, user_ftp*0.55, user_ftp*0.75, user_ftp*0.90, user_ftp*1.05, 10000]
                labels = ['Z1 Recupero', 'Z2 Resistenza', 'Z3 Tempo', 'Z4 Soglia', 'Z5+ VO2Max']
                colors_zones = ['#A0A0A0', '#00BFFF', '#32CD32', '#FFD700', '#FF4500']
                df['zone'] = pd.cut(df['power'], bins=bins, labels=labels)
                z_counts = df['zone'].value_counts(sort=False).reset_index()
                z_counts.columns = ['Zona', 'Sec']
                z_counts['Minuti'] = round(z_counts['Sec'] / 60, 1)
                fig_zones = px.bar(z_counts, x=(z_counts['Sec']/z_counts['Sec'].sum())*100, y='Zona', text='Minuti', orientation='h', color='Zona', color_discrete_sequence=colors_zones)
                fig_zones.update_traces(texttemplate='%{text} min', textposition='outside')
                fig_zones.update_layout(showlegend=False, template="plotly_white", xaxis_title="% Tempo", yaxis_title="")
                st.plotly_chart(fig_zones, use_container_width=True)

        # --- VELOCITÀ & ALTRI ---
        if 'speed_kmh' in df.columns:
            s_max, s_avg = df['speed_kmh'].max(), df['speed_kmh'].mean()
            st.subheader(f"📈 Velocità (Max: {s_max:.1f} km/h | Avg: {s_avg:.1f} km/h)")
            fig_spd = px.line(df, x=x_axis, y='speed_kmh', color_discrete_sequence=['#00BFFF'])
            fig_spd.update_layout(xaxis_title=x_label, template="plotly_white")
            st.plotly_chart(fig_spd, use_container_width=True)

        if 'cadence' in df.columns:
            cad_valid = df[df['cadence'] > 0]['cadence']
            cad_max = cad_valid.max() if not cad_valid.empty else 0
            st.subheader(f"🦵 Cadenza (Max: {int(cad_max)} rpm | Avg: {int(cad_avg)} rpm)")
            fig_cad = px.line(df, x=x_axis, y='cadence', color_discrete_sequence=['#32CD32'])
            fig_cad.update_layout(xaxis_title=x_label, yaxis_title="rpm", template="plotly_white")
            st.plotly_chart(fig_cad, use_container_width=True)

        if 'heart_rate' in df.columns:
            hr_max, hr_avg = df['heart_rate'].max(), df['heart_rate'].mean()
            st.subheader(f"❤️ Cardio (Max: {int(hr_max)} bpm | Avg: {int(hr_avg)} bpm)")
            fig_hr = px.line(df, x=x_axis, y='heart_rate', color_discrete_sequence=['red'])
            fig_hr.update_layout(xaxis_title=x_label, template="plotly_white")
            st.plotly_chart(fig_hr, use_container_width=True)

            # --- RELAZIONE FC / CADENZA / POTENZA ---
            if 'cadence' in df.columns and 'power' in df.columns:
                rel_df = df[['heart_rate', 'cadence', 'power']].dropna().copy()
                rel_df = rel_df[(rel_df['heart_rate'] > 0) & (rel_df['cadence'] > 0)]
                if not rel_df.empty:
                    hr_mean = rel_df['heart_rate'].mean()
                    cad_mean = rel_df['cadence'].mean()
                    p_mean = rel_df['power'].mean()
                    st.subheader(
                        f"❤️🦵 FC vs Cadenza (colore: Potenza) – Avg: {hr_mean:.0f} bpm, {cad_mean:.0f} rpm, {p_mean:.0f} W"
                    )

                    fig_rel = px.scatter(
                        rel_df,
                        x='cadence',
                        y='heart_rate',
                        color='power',
                        color_continuous_scale='Viridis',
                        labels={
                            'cadence': 'Cadenza (rpm)',
                            'heart_rate': 'Frequenza cardiaca (bpm)',
                            'power': 'Potenza (W)'
                        },
                        opacity=0.65
                    )
                    # Punto medio evidenziato in rosso e più grande
                    fig_rel.add_scatter(
                        x=[cad_mean],
                        y=[hr_mean],
                        mode="markers",
                        marker=dict(
                            color="red",
                            size=16,
                            line=dict(color="black", width=1.5)
                        ),
                        name="Media",
                        showlegend=False,
                    )

                    fig_rel.update_layout(template="plotly_white")
                    st.plotly_chart(fig_rel, use_container_width=True)
                else:
                    st.info("Dati insufficienti per il grafico FC/Cadenza/Potenza (valori mancanti o a zero).")

        if 'position_lat' in df.columns:
            st.subheader("🗺️ Mappa")
            map_df = df[['position_lat', 'position_long']].dropna()
            map_df['lat'] = map_df['position_lat'] * (180 / 2**31)
            map_df['lon'] = map_df['position_long'] * (180 / 2**31)
            st.map(map_df[['lat', 'lon']])

# ==============================================================================
# MODALITÀ 2: ANALISI TREND
# ==============================================================================
elif app_mode == "📈 Analisi Trend & Progressi":

    # Configurazione Trend nel menu laterale (come Analisi Singola)
    with st.sidebar:
        st.subheader("Configurazione Trend")
        trend_weight = st.number_input(
            "Peso (kg) per analisi trend",
            min_value=30,
            max_value=150,
            value=60,
            step=1,
            help="Peso corporeo usato per stimare le calorie totali e il rapporto W/kg nel tempo."
        )

        # FTP di default stimata sulle ultime 5 attività (stessa logica dell'analisi singola)
        trend_ftp_default = calculate_ftp_from_last_n_activities(files_dict, 5)
        trend_ftp = st.number_input(
            "FTP (W) per analisi trend",
            min_value=50,
            max_value=600,
            value=trend_ftp_default,
            step=5,
            help="Valore di FTP usato per stimare FC e cadenza medie a quella potenza in ogni sessione."
        )

    st.markdown("## 📈 I tuoi Progressi nel Tempo")
    
    with st.expander("📂 Seleziona le attività da analizzare", expanded=True):
        seleziona_tutti = st.checkbox("Seleziona tutti i file", value=True)
        if seleziona_tutti:
            files_scelti = all_files
        else:
            files_scelti = st.multiselect("Scegli i file:", all_files, default=all_files[:5])
    
    if st.button("🚀 Genera Analisi Trend"):
        if not files_scelti:
            st.warning("Seleziona almeno un file.")
        else:
            # Crea dizionario solo per i file selezionati (sempre da Google Drive)
            selected_files_dict = {name: files_dict[name] for name in files_scelti}
            with st.spinner('Analisi in corso...'):
                df_summary = get_activity_summary(selected_files_dict)
            
            if not df_summary.empty:
                # Ordiniamo per data
                df_summary = df_summary.sort_values(by="Data")

                # Calcolo kcal stimate per ogni uscita (stessa formula usata nell'analisi singola)
                if trend_weight > 0:
                    df_summary["Kcal stimate"] = df_summary["Distanza (km)"] * trend_weight * 0.3
                else:
                    df_summary["Kcal stimate"] = 0

                # Totali
                kcal_tot = df_summary["Kcal stimate"].sum()

                t1, t2, t3, t4, t5 = st.columns(5)
                t1.metric("Attività", len(df_summary))
                t2.metric("Km Totali", f"{int(df_summary['Distanza (km)'].sum())} km")
                t3.metric("Dislivello Tot", f"{int(df_summary['Dislivello (m)'].sum())} m")
                t4.metric("Ore Totali", f"{df_summary['Durata (min)'].sum()/60:.1f} h")
                t5.metric("Kcal Totali", f"{kcal_tot:.0f} kcal")
                
                st.markdown("---")
                
                # 1. Distanza
                st.subheader("📅 Volume: Distanza per Uscita")
                fig_vol = px.bar(df_summary, x='Data', y='Distanza (km)', 
                                 color='Dislivello (m)',
                                 color_continuous_scale='Bluered')
                fig_vol.update_layout(template="plotly_white")
                st.plotly_chart(fig_vol, use_container_width=True)
                
                # 2. Scatter Trends
                c_trend1, c_trend2 = st.columns(2)
                with c_trend1:
                    st.subheader("⚡ Trend Potenza")
                    fig_t_pwr = px.scatter(df_summary, x='Data', y='Potenza Avg (W)',
                                           size='Distanza (km)', color='Potenza Avg (W)',
                                           color_continuous_scale='Oranges')
                    fig_t_pwr.update_layout(template="plotly_white")
                    st.plotly_chart(fig_t_pwr, use_container_width=True)
                    
                with c_trend2:
                    st.subheader("📈 Trend Velocità")
                    fig_t_spd = px.scatter(df_summary, x='Data', y='Velocità Avg (km/h)',
                                           color='Velocità Avg (km/h)',
                                           color_continuous_scale='Tealgrn')
                    fig_t_spd.update_layout(template="plotly_white")
                    st.plotly_chart(fig_t_spd, use_container_width=True)

                # 3. Tabella
                with st.expander("Tabella Dati"):
                    st.dataframe(df_summary)
                    
                # 4. FTP stimata (proxy) e rapporto peso/potenza nel tempo
                if 'Potenza Avg (W)' in df_summary.columns and trend_weight > 0:
                    st.subheader("💪 FTP stimata (proxy) e W/kg nel tempo")

                    # Usiamo la potenza media come proxy di FTP per valutare il trend
                    trend_df = df_summary[["Data", "Potenza Avg (W)"]].copy()
                    trend_df["W/kg"] = trend_df["Potenza Avg (W)"] / trend_weight

                    # Tabella di sintesi del miglioramento
                    st.dataframe(
                        trend_df.rename(columns={
                            "Potenza Avg (W)": "Potenza media (W)",
                            "W/kg": "W/kg (media)"
                        }),
                        use_container_width=True
                    )

                    # Delta tra prima e ultima uscita come indicatore di miglioramento
                    first = trend_df.iloc[0]
                    last = trend_df.iloc[-1]
                    delta_ftp = last["Potenza Avg (W)"] - first["Potenza Avg (W)"]
                    delta_wkg = last["W/kg"] - first["W/kg"]

                    c1, c2 = st.columns(2)
                    c1.metric(
                        "Δ Potenza media (proxy FTP)",
                        f"{delta_ftp:+.0f} W",
                        help="Differenza tra la potenza media dell'ultima uscita e la prima nelle attività selezionate."
                    )
                    c2.metric(
                        "Δ W/kg",
                        f"{delta_wkg:+.2f} W/kg",
                        help="Differenza del rapporto peso/potenza medio (W/kg) tra prima e ultima uscita."
                    )

                # 5. Medie a FTP per ogni sessione (un punto per uscita, potenza fissata a trend_ftp)
                st.markdown("---")
                st.subheader("🔍 FC e Cadenza medie a FTP per ogni sessione")

                rows_ftp = []
                # Per ogni attività selezionata, ricalcoliamo FC e cadenza medie in prossimità di trend_ftp
                for _, row in df_summary.iterrows():
                    fname = row["Filename"]
                    file_id = selected_files_dict.get(fname)
                    if not file_id:
                        continue
                    df_act = load_single_fit_from_drive(file_id)
                    if df_act is None or df_act.empty:
                        continue
                    if not all(col in df_act.columns for col in ["power", "heart_rate", "cadence"]):
                        continue

                    # Finestra di tolleranza intorno a FTP (±5%)
                    low = trend_ftp * 0.95
                    high = trend_ftp * 1.05
                    m = (df_act["power"] >= low) & (df_act["power"] <= high)
                    sub = df_act[m]
                    sub = sub[(sub["heart_rate"] > 0) & (sub["cadence"] > 0)]
                    if sub.empty:
                        continue

                    rows_ftp.append({
                        "Data": row["Data"],
                        "FC a FTP (bpm)": sub["heart_rate"].mean(),
                        "Cadenza a FTP (rpm)": sub["cadence"].mean(),
                    })

                if rows_ftp:
                    ftp_trend_df = pd.DataFrame(rows_ftp).sort_values(by="Data")

                    # Grafico a punti: un punto per uscita, FC vs Cadenza (potenza fissata a FTP)
                    fig_ftp_rel = px.scatter(
                        ftp_trend_df,
                        x="Cadenza a FTP (rpm)",
                        y="FC a FTP (bpm)",
                        hover_data=["Data"],
                        color_discrete_sequence=["red"],
                        labels={
                            "Cadenza a FTP (rpm)": "Cadenza media a FTP (rpm)",
                            "FC a FTP (bpm)": "FC media a FTP (bpm)",
                        },
                        opacity=0.9,
                    )
                    fig_ftp_rel.update_layout(template="plotly_white")
                    st.plotly_chart(fig_ftp_rel, use_container_width=True)
            else:
                st.warning("Nessun dato valido trovato nei file selezionati (mancano campi come timestamp/distance/power, oppure i file sono vuoti).")
