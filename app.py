import json

import ee
import folium
import geopandas as gpd
import pandas as pd
import plotly.express as px
import requests
import streamlit as st
from streamlit_folium import st_folium

from palette_biome import (
    paleta_cores,      
    paleta_nomes,      
    dicionario_classes 
)

PROJETO_EE = "seu-projeto-id"

# Município fixo: São João de Meriti (RJ) - código IBGE 3305109
MUNICIPIO_NOME = "São João de Meriti - RJ"
MUNICIPIO_IBGE = "3305109"

# Asset da Coleção 10 do MapBiomas (bandas: classification_1985 ... classification_2024)
ASSET_MAPBIOMAS = (
    "projects/mapbiomas-public/assets/brazil/lulc/collection10/"
    "mapbiomas_brazil_collection10_coverage_v2"
)
ANOS = list(range(1985, 2025))

st.set_page_config(layout="wide", page_title=f"Uso do Solo - {MUNICIPIO_NOME}")

@st.cache_resource
def iniciar_ee():
    try:
        ee.Initialize(project=PROJETO_EE)
    except Exception:
        ee.Authenticate()
        ee.Initialize(project=PROJETO_EE)
    return True


iniciar_ee()

def carregar_imagem_mapbiomas(ano):
    return ee.Image(ASSET_MAPBIOMAS).select(f"classification_{ano}").rename("classification")


def criar_paleta_ee(cores):
    max_classe = max(cores.keys())
    palette = ["000000"] * (max_classe + 1)
    for codigo, cor in cores.items():
        palette[codigo] = cor.lstrip("#")
    return palette


palette_list = criar_paleta_ee(paleta_cores)
MAX_CLASSE = max(paleta_cores.keys())


def adicionar_camada_ee(mapa, ee_image, vis_params, nome):
    map_id = ee_image.getMapId(vis_params)
    folium.TileLayer(
        tiles=map_id["tile_fetcher"].url_format,
        attr="Google Earth Engine / MapBiomas",
        name=nome,
        overlay=True,
        control=True,
    ).add_to(mapa)


def limpar_geojson(gdf):
    if gdf.crs is not None and gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(epsg=4326)
    return json.loads(gdf[["geometry"]].to_json())


@st.cache_data(show_spinner=False)
def baixar_limite_municipio(codigo_ibge):
    """Baixa o contorno oficial do município na API de malhas do IBGE."""
    url = (
        f"https://servicodados.ibge.gov.br/api/v3/malhas/municipios/{codigo_ibge}"
        "?formato=application/vnd.geo+json&qualidade=maxima"
    )
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    gdf = gpd.GeoDataFrame.from_features(r.json()["features"], crs="EPSG:4326")
    return limpar_geojson(gdf)


@st.cache_data(show_spinner=False)
def calcular_estatisticas_area(ano, geojson_str):
    """Área (ha) por classe SÓ dentro do polígono do município."""
    region = ee.FeatureCollection(json.loads(geojson_str)).geometry()

    # Recorta a imagem no município: pixels fora ficam mascarados e não entram na conta
    image = carregar_imagem_mapbiomas(ano).clip(region)

    area_imagem = ee.Image.pixelArea().divide(1e4).rename("area").addBands(image)

    stats = area_imagem.reduceRegion(
        reducer=ee.Reducer.sum().group(groupField=1, groupName="class"),
        geometry=region,
        scale=30,
        maxPixels=1e10,
        tileScale=4,
    )
    return {
        "stats": stats.getInfo(),
        # área do polígono, para conferência
        "area_poligono_ha": region.area(1).divide(1e4).getInfo(),
    }


def converter_para_dataframe(stats):
    linhas = []
    for grupo in stats.get("groups", []):
        codigo = int(grupo["class"])
        linhas.append({
            "class": dicionario_classes.get(codigo, str(codigo)),
            "area_ha": round(grupo["sum"], 2),
        })
    df = pd.DataFrame(linhas)
    if not df.empty:
        df = df.sort_values("area_ha", ascending=False).reset_index(drop=True)
    return df

st.title(f"Análise de Uso do Solo — {MUNICIPIO_NOME}")
st.sidebar.header("Configurações")

ano_selecionado = st.sidebar.selectbox("Selecione o ano:", ANOS, index=len(ANOS) - 1)

geojson_dict = None
try:
    with st.spinner("Carregando limite do município..."):
        geojson_dict = baixar_limite_municipio(MUNICIPIO_IBGE)
except Exception as e:
    st.warning(
        f"Não consegui baixar o limite do município pela API do IBGE ({e}). "
        "Envie o GeoJSON de São João de Meriti manualmente:"
    )
    arquivo = st.sidebar.file_uploader("GeoJSON do município", type=["geojson", "json"])
    if arquivo is not None:
        geojson_dict = limpar_geojson(gpd.read_file(arquivo))

if geojson_dict is None:
    st.stop()

st.write("### Visualização do uso do solo")

m = folium.Map(location=[-22.80, -43.37], zoom_start=12, tiles=None)

folium.TileLayer(
    tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
    attr="Esri World Imagery",
    name="Satélite",
).add_to(m)

# Recorta a camada para aparecer só dentro do município
imagem = carregar_imagem_mapbiomas(ano_selecionado).clip(
    ee.FeatureCollection(geojson_dict).geometry()
)

adicionar_camada_ee(
    m,
    imagem,
    {"palette": palette_list, "min": 0, "max": MAX_CLASSE},
    f"Uso do Solo {ano_selecionado}",
)

folium.GeoJson(
    geojson_dict,
    name="Limite do município",
    style_function=lambda _: {"color": "yellow", "weight": 2, "fillOpacity": 0},
).add_to(m)

minx, miny, maxx, maxy = gpd.GeoDataFrame.from_features(geojson_dict["features"]).total_bounds
m.fit_bounds([[miny, minx], [maxy, maxx]])

folium.LayerControl().add_to(m)
st_folium(m, height=600, use_container_width=True, returned_objects=[])

try:
    with st.spinner("Calculando estatísticas..."):
        resultado = calcular_estatisticas_area(ano_selecionado, json.dumps(geojson_dict))
    stats_df = converter_para_dataframe(resultado["stats"])
    area_poligono_ha = resultado["area_poligono_ha"]
except Exception as e:
    st.error(f"Erro ao calcular estatísticas: {e}")
    st.stop()

if stats_df.empty:
    st.warning("Nenhum pixel encontrado dentro do município.")
else:
    col1, col2, col3 = st.columns([0.6, 1, 1])

    with col1:
        st.write(f"Área por classe (ha) — {ano_selecionado}:")
        st.dataframe(stats_df, use_container_width=True)
        total_ha = stats_df["area_ha"].sum()
        st.caption(
            f"Total das classes: {total_ha:,.0f} ha | "
            f"Área do polígono: {area_poligono_ha:,.0f} ha"
        )
        if abs(total_ha - area_poligono_ha) / area_poligono_ha > 0.05:
            st.warning("O total das classes difere do polígono em mais de 5%. Verifique o limite carregado.")

    with col2:
        fig_pie = px.pie(
            stats_df,
            names="class",
            values="area_ha",
            color="class",
            color_discrete_map=paleta_nomes,
            title="Distribuição de Áreas por Classe",
        )
        st.plotly_chart(fig_pie, use_container_width=True)

    with col3:
        fig_bar = px.bar(
            stats_df,
            x="class",
            y="area_ha",
            labels={"class": "Classe", "area_ha": "Área (ha)"},
            title="Área por Classe",
            color="class",
            color_discrete_map=paleta_nomes,
        )
        st.plotly_chart(fig_bar, use_container_width=True)