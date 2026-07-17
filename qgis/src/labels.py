from pathlib import Path
import re

from qgis.PyQt.QtCore import QVariant
from qgis.core import (
    NULL,
    Qgis,
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsProject,
    QgsVectorFileWriter,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.utils import iface


# =============================================================================
# CONFIGURATION
# =============================================================================

OUTPUT_DIRECTORY = Path(
    r"F:\[French vACC] Navigation & Operations"
    r"\GitHub Projects\qgis-diagrams\qgis\src\labels"
)

OUTPUT_PREFIX = "CCA"

# Default values used by the label styling.
DEFAULT_STYLE = 10
DEFAULT_BUFFER = 1


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def safe_filename(value: str) -> str:
    """
    Convert a layer or group name into a safe filename component.
    """
    value = value.strip()
    value = re.sub(r"\s+", "_", value)
    value = re.sub(r'[<>:"/\\|?*]', "_", value)
    value = re.sub(r"_+", "_", value)
    return value.strip("_")


def find_icao_group(layer):
    """
    Walk upward through the QGIS Layers tree and return the nearest group
    whose name is a four-letter ICAO code.
    """
    root = QgsProject.instance().layerTreeRoot()
    layer_node = root.findLayer(layer.id())

    if layer_node is None:
        raise RuntimeError(
            "The active layer could not be found in the QGIS Layers tree."
        )

    node = layer_node.parent()

    while node is not None:
        group_name = node.name().strip().upper()

        if re.fullmatch(r"[A-Z]{4}", group_name):
            return group_name, node

        node = node.parent()

    raise RuntimeError(
        "No four-letter ICAO group was found above the active layer.\n\n"
        "Place the polygon layer inside a group such as LFBD or LFBZ."
    )


def get_or_create_labels_group(icao_group_node):
    """
    Find or create a 'Labels' group inside the ICAO group.
    """
    for child in icao_group_node.children():
        if child.nodeType() == child.NodeGroup:
            if child.name().strip().lower() == "labels":
                return child

    return icao_group_node.insertGroup(0, "Labels")


def normalize_layer_source(source: str):
    """
    Extract the physical file path from a QGIS layer source string.
    """
    if not source:
        return None

    physical_source = source.split("|", 1)[0]

    try:
        return Path(physical_source).resolve()
    except Exception:
        return None


def remove_existing_output_layer(output_path: Path):
    """
    Remove an existing output layer from the QGIS project so Windows does not
    keep the GeoJSON file locked.
    """
    project = QgsProject.instance()
    target_path = output_path.resolve()

    for project_layer in list(project.mapLayers().values()):
        source_path = normalize_layer_source(project_layer.source())

        if source_path is not None and source_path == target_path:
            project.removeMapLayer(project_layer.id())


def source_value(feature, field_names, field_name, default=None):
    """
    Safely read a source-layer attribute.
    """
    if field_name not in field_names:
        return default

    value = feature[field_name]

    if value is None or value == NULL:
        return default

    return value


# =============================================================================
# GET ACTIVE POLYGON LAYER
# =============================================================================

source_layer = iface.activeLayer()

if source_layer is None:
    raise RuntimeError(
        "No active layer is selected.\n\n"
        "Select the polygon layer in the Layers panel and run the script again."
    )

if not isinstance(source_layer, QgsVectorLayer):
    raise RuntimeError("The active layer is not a vector layer.")

if not source_layer.isValid():
    raise RuntimeError("The active vector layer is invalid.")

if QgsWkbTypes.geometryType(source_layer.wkbType()) != Qgis.GeometryType.Polygon:
    raise RuntimeError(
        f"The active layer '{source_layer.name()}' is not a polygon layer."
    )


# =============================================================================
# DETERMINE ICAO CODE AND OUTPUT NAME
# =============================================================================

icao_code, icao_group_node = find_icao_group(source_layer)

polygon_name = safe_filename(source_layer.name())
icao_code = safe_filename(icao_code)

if not polygon_name:
    raise RuntimeError("The active polygon layer has no usable name.")

output_filename = (
    f"{OUTPUT_PREFIX}_{icao_code}_{polygon_name}_Labels.geojson"
)

output_path = OUTPUT_DIRECTORY / output_filename


# =============================================================================
# CREATE OUTPUT MEMORY LAYER
# =============================================================================

crs_authid = source_layer.crs().authid()

if not crs_authid:
    raise RuntimeError(
        f"The source layer '{source_layer.name()}' has no valid CRS."
    )

memory_layer = QgsVectorLayer(
    f"Point?crs={crs_authid}",
    output_path.stem,
    "memory",
)

if not memory_layer.isValid():
    raise RuntimeError("Could not create the temporary label layer.")

provider = memory_layer.dataProvider()

output_fields = [
    QgsField("fir", QVariant.String, len=16),
    QgsField("identifier", QVariant.String, len=254),
    QgsField("subsector_id", QVariant.String, len=100),
    QgsField("name", QVariant.String, len=100),
    QgsField("level_lower", QVariant.Int),
    QgsField("level_upper", QVariant.Int),
    QgsField("callout_x1", QVariant.Double),
    QgsField("callout_y1", QVariant.Double),
    QgsField("callout_x2", QVariant.Double),
    QgsField("callout_y2", QVariant.Double),
    QgsField("style", QVariant.Int),
    QgsField("buffer", QVariant.Int),
]

provider.addAttributes(output_fields)
memory_layer.updateFields()

source_field_names = {
    field.name()
    for field in source_layer.fields()
}


# =============================================================================
# CHOOSE FEATURES
# =============================================================================

selected_features = source_layer.selectedFeatures()

if selected_features:
    source_features = selected_features
    selection_description = (
        f"{len(selected_features)} selected feature(s)"
    )
else:
    source_features = list(source_layer.getFeatures())
    selection_description = (
        f"all {len(source_features)} feature(s)"
    )

if not source_features:
    raise RuntimeError(
        f"The layer '{source_layer.name()}' contains no usable features."
    )


# =============================================================================
# GENERATE LABEL POINTS
# =============================================================================

new_features = []
skipped_features = 0

for source_feature in source_features:
    source_geometry = source_feature.geometry()

    if source_geometry is None or source_geometry.isEmpty():
        skipped_features += 1
        continue

    # Point on surface is preferable to a centroid because it remains inside
    # concave polygons.
    label_geometry = source_geometry.pointOnSurface()

    if label_geometry is None or label_geometry.isEmpty():
        skipped_features += 1
        continue

    label_point = label_geometry.asPoint()

    source_identifier = source_value(
        source_feature,
        source_field_names,
        "identifier",
        f"{icao_code}:{polygon_name}",
    )

    source_name = source_value(
        source_feature,
        source_field_names,
        "name",
        source_layer.name(),
    )

    source_fir = source_value(
        source_feature,
        source_field_names,
        "fir",
        icao_code,
    )

    source_level_lower = source_value(
        source_feature,
        source_field_names,
        "level_lower",
        None,
    )

    source_level_upper = source_value(
        source_feature,
        source_field_names,
        "level_upper",
        None,
    )

    label_feature = QgsFeature(memory_layer.fields())
    label_feature.setGeometry(QgsGeometry.fromPointXY(label_point))

    label_feature.setAttributes([
        source_fir,
        str(source_identifier),
        "",
        str(source_name),
        source_level_lower,
        source_level_upper,

        # Initial callout coordinates.
        #
        # x1/y1 represent the label point or callout bend.
        # x2/y2 can later be moved to the desired polygon boundary point.
        label_point.x(),
        label_point.y(),
        label_point.x(),
        label_point.y(),

        DEFAULT_STYLE,
        DEFAULT_BUFFER,
    ])

    new_features.append(label_feature)


if not new_features:
    raise RuntimeError(
        "No label points could be generated from the source polygons."
    )

provider.addFeatures(new_features)
memory_layer.updateExtents()


# =============================================================================
# PREPARE OUTPUT LOCATION
# =============================================================================

OUTPUT_DIRECTORY.mkdir(parents=True, exist_ok=True)

remove_existing_output_layer(output_path)

if output_path.exists():
    try:
        output_path.unlink()
    except PermissionError as error:
        raise RuntimeError(
            f"Could not overwrite the existing file:\n"
            f"{output_path}\n\n"
            "The file may still be open or locked by another application."
        ) from error


# =============================================================================
# SAVE AS GEOJSON
# =============================================================================

save_options = QgsVectorFileWriter.SaveVectorOptions()
save_options.driverName = "GeoJSON"
save_options.fileEncoding = "UTF-8"

write_result = QgsVectorFileWriter.writeAsVectorFormatV3(
    memory_layer,
    str(output_path),
    QgsProject.instance().transformContext(),
    save_options,
)

writer_error = write_result[0]

if writer_error != QgsVectorFileWriter.NoError:
    raise RuntimeError(
        "Could not save the label layer.\n\n"
        f"Output:\n{output_path}\n\n"
        f"Writer result:\n{write_result}"
    )


# =============================================================================
# LOAD OUTPUT INTO THE ICAO LABELS GROUP
# =============================================================================

saved_layer = QgsVectorLayer(
    str(output_path),
    output_path.stem,
    "ogr",
)

if not saved_layer.isValid():
    raise RuntimeError(
        "The GeoJSON file was written, but QGIS could not load it:\n"
        f"{output_path}"
    )

project = QgsProject.instance()
labels_group = get_or_create_labels_group(icao_group_node)

# Add without automatically placing the layer at the project root.
project.addMapLayer(saved_layer, False)
labels_group.addLayer(saved_layer)

iface.setActiveLayer(saved_layer)
saved_layer.triggerRepaint()


# =============================================================================
# RESULT
# =============================================================================

print("=" * 72)
print("LABEL LAYER CREATED")
print("=" * 72)
print(f"Source layer:     {source_layer.name()}")
print(f"ICAO group:       {icao_code}")
print(f"Features used:    {selection_description}")
print(f"Labels created:   {len(new_features)}")
print(f"Features skipped: {skipped_features}")
print(f"Output filename:  {output_filename}")
print(f"Output location:  {output_path}")
print("=" * 72)

iface.messageBar().pushSuccess(
    "Label layer created",
    f"{output_filename} — {len(new_features)} label(s)",
)