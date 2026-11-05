# Capas públicas incluidas (`datos_publicos/`)

Todas recortadas a la Región Metropolitana (polígono de región DPA 2023, `CUT_REG = 13`), en EPSG:4326 y formato GeoPackage. Solo se incluyen capas cuyo origen y licencia están verificados.

| Archivo | Fuente | Fecha | Licencia / atribución | Uso en el proyecto |
|---|---|---|---|---|
| `dpa2023_rm.gpkg` (`region_rm`, `provincias_rm`, `comunas_rm`) | SUBDERE, con IGM, DIFROL e INE (IDE Chile) | 03-08-2023 | Toda publicación debe llevar la leyenda: "Autorizada su circulación por Resolución N° 87 del 2023 de la Dirección Nacional de Fronteras y Límites del Estado". | Ubicar los lotes de prueba (`tools/elegir_pilotos_rm.py`) y recorte regional |
| `osm_edificios_rm.gpkg`, `osm_puntos_rm.gpkg` | © colaboradores de OpenStreetMap (extractos BBBike) | 31-05-2023 | ODbL 1.0: "© OpenStreetMap contributors", openstreetmap.org/copyright | Contexto; la cobertura de edificios en el Gran Santiago es casi nula |
| `bcn_red_ferroviaria_rm.gpkg` | Biblioteca del Congreso Nacional de Chile, SIIT | 2019 | Uso libre citando a la BCN como fuente; material referencial, sin precisión geodésica | Contexto (barreras lineales) |
| `huellas_referencia_ot.json` | Huellas de cobertura de las nubes de puntos de referencia de OpenTopography | 2026 | Solo la geometría de cobertura; las nubes de puntos no se incluyen | Saber qué lotes tienen referencia fina para la auditoría |

Las fuentes de elevación (Copernicus GLO-30, AW3D30, ICESat-2, GEDI, ESA WorldCover) se descargan en tiempo de ejecución y no se redistribuyen. Sus términos están en [`docs/licencias/`](../docs/licencias/).
