import logging
import os
import tempfile
import typing as t
from collections import OrderedDict
from logging import log

import dash_bootstrap_components as dbc
import numpy as np
import plotly.graph_objects as go
from anndata import AnnData
from Bio import Phylo
from dash import Dash, State, dcc, html
from dash.dependencies import Input, Output
from dash.development.base_component import Component
from plotly.express.colors import sample_colorscale

from cellarium.cas.postprocessing import (
    CAS_CL_SCORES_ANNDATA_OBSM_KEY,
    CellOntologyScoresAggregationDomain,
    CellOntologyScoresAggregationOp,
    convert_aggregated_cell_ontology_scores_to_rooted_tree,
    generate_phyloxml_from_scored_cell_ontology_tree,
    get_aggregated_cas_ontology_aware_scores,
    get_obs_indices_for_cluster,
    insert_cas_ontology_aware_response_into_adata,
)
from cellarium.cas.postprocessing.cell_ontology import CL_CELL_ROOT_NODE, CellOntologyCache
from cellarium.cas.visualization._components.circular_tree_plot import CircularTreePlot
from cellarium.cas.visualization.ui_utils import ConfigValue, find_and_kill_process

# cell type ontology terms (and all descendents) to hide from the visualization
DEFAULT_HIDDEN_CL_NAMES_SET = {}


class DomainSelectionConstants:
    NONE = 0
    USER_SELECTION = 1
    SEPARATOR = 2


# cell type ontology terms to always show as text labels in the visualization
DEFAULT_SHOWN_CL_NAMES_SET = {
    "CL_0000236",
    "CL_0000084",
    "CL_0000789",
    "CL_0000798",
    "CL_0002420",
    "CL_0002419",
    "CL_0000786",
    "CL_0000576",
    "CL_0001065",
    "CL_0000451",
    "CL_0000094",
    "CL_0000235",
    "CL_0000097",
    "CL_0000814",
    "CL_0000827",
    "CL_0000066",
    "CL_0000163",
    "CL_0000151",
    "CL_0000064",
    "CL_0000322",
    "CL_0000076",
    "CL_0005006",
    "CL_0000148",
    "CL_0000646",
    "CL_0009004",
    "CL_0000115",
    "CL_0000125",
    "CL_0002319",
    "CL_0000187",
    "CL_0000057",
    "CL_0008034",
    "CL_0000092",
    "CL_0000058",
    "CL_0000060",
    "CL_0000136",
    "CL_0000499",
    "CL_0000222",
    "CL_0007005",
    "CL_0000039",
    "CL_0000019",
    "CL_0000223",
    "CL_0008019",
    "CL_0005026",
    "CL_0000182",
    "CL_0000023",
    "CL_0000679",
    "CL_0000126",
    "CL_0000540",
    "CL_0000127",
    "CL_0011005",
}


class CASCircularTreePlotUMAPDashApp:
    ALL_CELLS_DOMAIN_KEY = "all cells"
    CLUSTER_PREFIX_DOMAIN_KEY = "cluster "

    def __init__(
        self,
        adata: AnnData,
        cas_ontology_aware_response: list,
        cluster_label_obs_column: t.Optional[str] = None,
        aggregation_op: CellOntologyScoresAggregationOp = CellOntologyScoresAggregationOp.MEAN,
        aggregation_domain: CellOntologyScoresAggregationDomain = CellOntologyScoresAggregationDomain.OVER_THRESHOLD,
        score_threshold: float = 0.05,
        min_cell_fraction: float = 0.01,
        umap_marker_size: float = 3.0,
        umap_padding: float = 0.15,
        umap_min_opacity: float = 0.1,
        umap_max_opacity: float = 1.0,
        umap_inactive_cell_color: str = "rgb(180,180,180)",
        umap_inactive_cell_opacity: float = 0.5,
        umap_active_cell_color: str = "rgb(250,50,50)",
        umap_default_cell_color: str = "rgb(180,180,180)",
        umap_default_opacity: float = 0.9,
        circular_tree_plot_linecolor: str = "rgb(200,200,200)",
        circular_tree_start_angle: int = 180,
        circular_tree_end_angle: int = 360,
        figure_height: int = 400,
        hidden_cl_names_set: set[str] = DEFAULT_HIDDEN_CL_NAMES_SET,
        shown_cl_names_set: set[str] = DEFAULT_SHOWN_CL_NAMES_SET,
        score_colorscale: t.Union[str, list] = "Viridis",
    ):
        self.adata = adata
        self.aggregation_op = aggregation_op
        self.aggregation_domain = aggregation_domain
        self.score_threshold = ConfigValue(score_threshold)
        self.min_cell_fraction = ConfigValue(min_cell_fraction)
        self.umap_min_opacity = umap_min_opacity
        self.umap_max_opacity = umap_max_opacity
        self.umap_marker_size = umap_marker_size
        self.umap_padding = umap_padding
        self.umap_inactive_cell_color = umap_inactive_cell_color
        self.umap_inactive_cell_opacity = umap_inactive_cell_opacity
        self.umap_active_cell_color = umap_active_cell_color
        self.umap_default_cell_color = umap_default_cell_color
        self.umap_default_opacity = umap_default_opacity
        self.circular_tree_plot_linecolor = circular_tree_plot_linecolor
        self.circular_tree_start_angle = circular_tree_start_angle
        self.circular_tree_end_angle = circular_tree_end_angle
        self.height = figure_height
        self.hidden_cl_names_set = hidden_cl_names_set
        self.shown_cl_names_set = shown_cl_names_set
        self.score_colorscale = score_colorscale

        assert "X_umap" in adata.obsm, "UMAP coordinates not found in adata.obsm['X_umap']"

        # setup cell domains
        self.cell_domain_map = OrderedDict()
        self.cell_domain_map[self.ALL_CELLS_DOMAIN_KEY] = np.arange(adata.n_obs)
        if cluster_label_obs_column is not None:
            assert cluster_label_obs_column in adata.obs
            for cluster_label in adata.obs[cluster_label_obs_column].cat.categories:
                self.cell_domain_map[self.CLUSTER_PREFIX_DOMAIN_KEY + cluster_label] = get_obs_indices_for_cluster(
                    adata, cluster_label_obs_column, cluster_label
                )

        # default cell domain
        self.selected_cell_domain_key = ConfigValue(DomainSelectionConstants.NONE)
        self.selected_cells = []

        # instantiate the cell type ontology cache
        self.cl = CellOntologyCache()

        # insert CA ontology-aware response into adata
        insert_cas_ontology_aware_response_into_adata(cas_ontology_aware_response, adata, self.cl)

        # instantiate the Dash app
        self.app = Dash(__name__, external_stylesheets=[dbc.themes.BOOTSTRAP, dbc.icons.BOOTSTRAP])
        self.server = self.app.server
        self.app.layout = self._create_layout()
        self._setup_initialization()
        self._setup_callbacks()

    def run(self, port: int = 8050, **kwargs):
        log(logging.INFO, "Starting Dash application...")
        try:
            self.app.run_server(port=port, jupyter_mode="inline", jupyter_height=self.height + 100, **kwargs)
        except OSError:  # Dash raises OSError if the port is already in use
            find_and_kill_process(port)
            self.app.run_server(port=port, jupyter_mode="inline", jupyter_height=self.height + 100, **kwargs)

    def _instantiate_circular_tree_plot(self) -> CircularTreePlot:
        # reduce scores over the provided cells
        selected_cells = self._get_effective_selected_cells()
        aggregated_scores = get_aggregated_cas_ontology_aware_scores(
            self.adata,
            obs_indices=(
                self.cell_domain_map[self.ALL_CELLS_DOMAIN_KEY] if len(selected_cells) == 0 else selected_cells
            ),
            aggregation_op=self.aggregation_op,
            aggregation_domain=self.aggregation_domain,
            threshold=self.score_threshold.get(),
        )

        # generate a Phylo tree
        rooted_tree = convert_aggregated_cell_ontology_scores_to_rooted_tree(
            aggregated_scores=aggregated_scores,
            cl=self.cl,
            root_cl_name=CL_CELL_ROOT_NODE,
            min_fraction=self.min_cell_fraction.get(),
            hidden_cl_names_set=self.hidden_cl_names_set,
        )
        phyloxml_string = generate_phyloxml_from_scored_cell_ontology_tree(
            rooted_tree, "Scored cell type ontology tree", self.cl, indent=3
        )

        with tempfile.NamedTemporaryFile(delete=False, mode="w+t") as temp_file:
            temp_file_name = temp_file.name
            temp_file.write(phyloxml_string)
            temp_file.flush()

        try:
            phyloxml_tree = Phylo.read(temp_file_name, "phyloxml")
        finally:
            os.remove(temp_file_name)

        return CircularTreePlot(
            tree=phyloxml_tree,
            score_colorscale=self.score_colorscale,
            linecolor=self.circular_tree_plot_linecolor,
            start_angle=self.circular_tree_start_angle,
            end_angle=self.circular_tree_end_angle,
            shown_cl_names_set=self.shown_cl_names_set,
        )

    def _get_padded_umap_bounds(self, umap_padding: float) -> t.Tuple[float, float, float, float]:
        actual_min_x = np.min(self.adata.obsm["X_umap"][:, 0])
        actual_max_x = np.max(self.adata.obsm["X_umap"][:, 0])
        actual_min_y = np.min(self.adata.obsm["X_umap"][:, 1])
        actual_max_y = np.max(self.adata.obsm["X_umap"][:, 1])
        padded_min_x = actual_min_x - umap_padding * (actual_max_x - actual_min_x)
        padded_max_x = actual_max_x + umap_padding * (actual_max_x - actual_min_x)
        padded_min_y = actual_min_y - umap_padding * (actual_max_y - actual_min_y)
        padded_max_y = actual_max_y + umap_padding * (actual_max_y - actual_min_y)

        return padded_min_x, padded_max_x, padded_min_y, padded_max_y

    def _get_scores_for_cl_name(self, cl_name: str) -> np.ndarray:
        cl_index = self.cl.cl_names_to_idx_map[cl_name]
        return self.adata.obsm[CAS_CL_SCORES_ANNDATA_OBSM_KEY][:, cl_index].toarray().flatten()

    def _get_scatter_plot_opacity_from_scores(self, scores: np.ndarray) -> np.ndarray:
        min_score = np.min(scores)
        max_score = np.max(scores)
        normalized_scores = (scores - min_score) / (1e-6 + max_score - min_score)
        return np.maximum(
            scores, self.umap_min_opacity + (self.umap_max_opacity - self.umap_min_opacity) * normalized_scores
        )

    def _create_layout(self):
        # Custom JavaScript for increasing scroll zoom sensitivity (doesn't seem to work)
        scroll_zoom_js = """
        function(graph) {
            var plot = document.getElementById(graph.id);
            plot.on('wheel', function(event) {
                event.deltaY *= 0.2;  // Adjust this value to change sensitivity
            });
            return graph;
        }
        """

        layout = html.Div(
            [
                dbc.Row(dbc.Col(className="gr-spacer", width=12)),
                dbc.Row(
                    dbc.Col(
                        [
                            html.H3(self._render_breadcrumb(), id="selected-domain-label", className="gr-breadcrumb"),
                            html.Div(
                                [
                                    dbc.ButtonGroup(
                                        [
                                            dbc.Button(
                                                html.I(className="bi bi-gear-fill"),
                                                id="settings-button",
                                                n_clicks=0,
                                                size="sm",
                                            ),
                                        ]
                                    )
                                ],
                                className="gr-settings-buttons",
                            ),
                        ],
                        className="gr-title",
                        width=12,
                    )
                ),
                dbc.Row(
                    [
                        dbc.Col(
                            html.Div(
                                [
                                    html.Div("Ontology View", className="gr-header"),
                                    dcc.Graph(
                                        id="circular-tree-plot",
                                        style={
                                            "width": "100%",
                                            "display": "inline-block",
                                            "height": f"{self.height}px",
                                        },
                                        config={"scrollZoom": True},
                                    ),
                                ]
                            ),
                            width=6,
                        ),
                        dbc.Col(
                            html.Div(
                                [
                                    html.Div("UMAP View", className="gr-header"),
                                    dcc.Graph(
                                        id="umap-scatter-plot",
                                        style={
                                            "width": "100%",
                                            "display": "inline-block",
                                            "height": f"{self.height-10}px",
                                        },
                                        config={"scrollZoom": True},
                                    ),
                                ]
                            ),
                            width=6,
                        ),
                    ]
                ),
                dbc.Offcanvas(
                    id="settings-pane", title="Settings", is_open=False, children=self._render_closed_settings_pane()
                ),
                html.Div(id="init", style={"display": "none"}),
                html.Div(id="no-action", style={"display": "none"}),
                html.Script(scroll_zoom_js),
            ],
        )

        return layout

    def _initialize_umap_scatter_plot(self) -> go.Figure:
        # calculate static bounds for the UMAP scatter plot
        self.umap_min_x, self.umap_max_x, self.umap_min_y, self.umap_max_y = self._get_padded_umap_bounds(
            self.umap_padding
        )

        fig = go.Figure()

        color = self.umap_default_cell_color

        selected_cells = self._get_effective_selected_cells()

        if len(selected_cells) > 0:
            color = [self.umap_inactive_cell_color] * self.adata.n_obs
            for i_obs in selected_cells:
                color[i_obs] = self.umap_active_cell_color

        fig.add_trace(
            go.Scatter(
                x=self.adata.obsm["X_umap"][:, 0],
                y=self.adata.obsm["X_umap"][:, 1],
                mode="markers",
                marker=dict(
                    color=color,
                    size=self.umap_marker_size,
                    opacity=self.umap_default_opacity,
                ),
            )
        )
        self._update_umap_scatter_plot_layout(fig)
        self._umap_scatter_plot_figure = fig
        return fig

    def _initialize_circular_tree_plot(self) -> go.Figure:
        self.circular_tree_plot = self._instantiate_circular_tree_plot()
        fig = self.circular_tree_plot.plotly_figure
        self._circular_tree_plot_figure = fig
        return fig

    def _setup_initialization(self):
        @self.app.callback(Output("umap-scatter-plot", "figure"), Input("init", "children"))
        def _initialize_umap_scatter_plot(init):
            return self._initialize_umap_scatter_plot()

        @self.app.callback(Output("circular-tree-plot", "figure"), Input("init", "children"))
        def _initialize_circular_tree_plot(init):
            return self._initialize_circular_tree_plot()

    def _update_umap_scatter_plot_layout(self, umap_scatter_plot_fig):
        umap_scatter_plot_fig.update_layout(
            plot_bgcolor="white",
            margin=dict(l=0, r=25, t=50, b=0),
            xaxis=dict(
                title="UMAP 1",
                showgrid=False,
                zeroline=False,  # Keep zero line enabled
                zerolinecolor="black",
                range=[self.umap_min_x, self.umap_max_x],  # Set x-axis limits
                showline=True,  # Show axis line
                linecolor="black",
                linewidth=1,
                tickmode="linear",
                tick0=-10,
                dtick=5,
            ),
            yaxis=dict(
                title="UMAP 2",
                showgrid=False,
                zeroline=False,  # Keep zero line enabled
                zerolinecolor="black",
                range=[self.umap_min_y, self.umap_max_y],  # Set y-axis limits
                showline=True,  # Show axis line
                linecolor="black",
                linewidth=1,
                tickmode="linear",
                tick0=-10,
                dtick=5,
            ),
            dragmode="pan",
        )

    def _render_breadcrumb(self) -> Component:
        selected_cells = self._get_effective_selected_cells()
        if len(selected_cells) == 0 and self.selected_cell_domain_key.get() == DomainSelectionConstants.NONE:
            label = "Viewing results for all cells"
            show_clear = False
        elif (
            len(selected_cells) == 1 and self.selected_cell_domain_key.get() == DomainSelectionConstants.USER_SELECTION
        ):
            label = f"Selected cell index {selected_cells[0]}"
            show_clear = True
        elif len(selected_cells) > 1 and self.selected_cell_domain_key.get() == DomainSelectionConstants.USER_SELECTION:
            label = f"Selected {len(selected_cells)} cells"
            show_clear = True
        else:
            modifier = "cell" if len(selected_cells) == 1 else "cells"
            label = f"Selected cell domain {self.selected_cell_domain_key.get()} ({len(selected_cells)} {modifier})"
            show_clear = True
        children = [html.B(label, className="gr-breadcrumb-label")]

        if show_clear:
            children.append(
                html.Div(
                    [html.I(className="bi bi-x-circle")],
                    id="reset-selection-button",
                    n_clicks=0,
                    className="btn btn-link",
                    title="Clear selection",
                )
            )
        return html.Div(children)

    def _render_closed_settings_pane(self) -> Component:
        return [
            html.Div(
                [
                    html.Label("Cell selection:", style={"margin-bottom": "5px"}),
                    self._render_domain_dropdown(),
                ],
                className="gr-form-item",
            ),
            html.Div(
                [
                    dbc.Label("Evidence threshold:", html_for="evidence-threshold"),
                    dcc.Slider(
                        id="evidence-threshold",
                        min=0,
                        max=1,
                        value=self.score_threshold.get(dirty_read=True),
                        marks={
                            0: "0",
                            0.25: "0.25",
                            0.5: "0.5",
                            0.75: "0.75",
                            1: "1",
                        },
                        tooltip={"placement": "bottom", "always_visible": True, "style": {"margin": "0 5px"}},
                    ),
                ],
                className="gr-form-item",
            ),
            html.Div(
                [
                    dbc.Label("Minimum cell fraction:", html_for="cell-fraction"),
                    dcc.Slider(
                        id="cell-fraction",
                        min=0,
                        max=1,
                        value=self.min_cell_fraction.get(dirty_read=True),
                        marks={
                            0: "0",
                            0.25: "0.25",
                            0.5: "0.5",
                            0.75: "0.75",
                            1: "1",
                        },
                        tooltip={"placement": "bottom", "always_visible": True, "style": {"margin": "0 5px"}},
                    ),
                ],
                className="gr-form-item",
            ),
            html.Div(
                [
                    dbc.Button(
                        "Cancel",
                        id="cancel-button",
                        title="Cancel the changes and close the settings pane",
                        n_clicks=0,
                    ),
                    dbc.Button(
                        "Update",
                        id="update-button",
                        title="Update the graphs based on the specified configuration",
                        n_clicks=0,
                    ),
                ],
                className="gr-settings-button-bar",
            ),
            html.A(
                html.Img(
                    src="assets/cellarium-powered-400px.png",
                ),
                href="https://cellarium.ai",
                className="gr-powered-by",
                target="_blank",
            ),
        ]

    def _render_domain_dropdown(self) -> Component:
        labels = [{"label": "None selected", "value": DomainSelectionConstants.NONE}]
        if len(self.selected_cells) > 0:
            labels.append({"label": "User selection", "value": DomainSelectionConstants.USER_SELECTION})

        if len(self.cell_domain_map.keys()) > 1:
            labels.append({"label": "________________", "value": None, "disabled": True})
            labels.append({"label": html.Span("Provided domains"), "value": None, "disabled": True})

            for k in list(self.cell_domain_map.keys())[1:]:
                labels.append({"label": k, "value": k})

        return dcc.Dropdown(
            id="domain-dropdown",
            options=labels,
            value=self.selected_cell_domain_key.get(),  # default to no selection
            className="gr-custom-dropdown",
            clearable=False,
        )

    def _get_effective_selected_cells(self) -> list:
        # User has chosen not to show any highlighted cells
        if self.selected_cell_domain_key.get() == DomainSelectionConstants.NONE:
            return []

        # User has chose to highlight explicitly selected cells
        if self.selected_cell_domain_key.get() == DomainSelectionConstants.USER_SELECTION:
            return self.selected_cells

        # User has chose to highlight pre-calculated domain cells
        if self.selected_cell_domain_key.get() is not None:
            return self.cell_domain_map[self.selected_cell_domain_key.get()]

    def _clear_selection(self):
        self.selected_cells = []
        self.selected_cell_domain_key.reset()

    def _setup_callbacks(self) -> None:
        # Cell selection callbacks
        @self.app.callback(
            Output("umap-scatter-plot", "figure", allow_duplicate=True),
            Input("circular-tree-plot", "clickData"),
            prevent_initial_call=True,
        )
        def _update_umap_scatter_plot_based_on_circular_tree_plot(clickData):
            if clickData is None or "points" not in clickData:
                return self._umap_scatter_plot_figure

            point = clickData["points"][0]
            if "pointIndex" not in point:
                return self._umap_scatter_plot_figure

            node_index = point["pointIndex"]
            cl_name = self.circular_tree_plot.clade_index_to_cl_name_map.get(node_index)
            if cl_name is None:
                return self._umap_scatter_plot_figure

            scores = self._get_scores_for_cl_name(cl_name)
            opacity = self._get_scatter_plot_opacity_from_scores(scores)
            color = sample_colorscale(self.circular_tree_plot.score_colorscale, scores)
            selected_cells_set = set(self._get_effective_selected_cells())
            for i_obs in range(self.adata.n_obs):
                if i_obs not in selected_cells_set:
                    color[i_obs] = self.umap_inactive_cell_color
                    opacity[i_obs] = self.umap_inactive_cell_opacity

            self._umap_scatter_plot_figure.update_traces(
                marker=dict(
                    color=color,
                    colorscale=self.circular_tree_plot.score_colorscale,
                    opacity=opacity,
                    cmin=0.0,
                    cmax=1.0,
                ),
                text=[f"{score:.5f}" for score in scores],
                hovertemplate="<b>Evidence score: %{text}</b><extra></extra>",
            )
            self._umap_scatter_plot_figure.update_layout(title=self.cl.cl_names_to_labels_map[cl_name])

            return self._umap_scatter_plot_figure

        @self.app.callback(
            Output("circular-tree-plot", "figure", allow_duplicate=True),
            Output("umap-scatter-plot", "figure", allow_duplicate=True),
            Output("selected-domain-label", "children", allow_duplicate=True),
            Input("umap-scatter-plot", "clickData"),
            prevent_initial_call=True,
        )
        def _update_circular_tree_plot_based_on_umap_scatter_plot(clickData):
            if clickData is None or "points" not in clickData:
                return self._circular_tree_plot_figure, self._umap_scatter_plot_figure, self._render_breadcrumb()

            point = clickData["points"][0]

            if "pointIndex" not in point:
                return self._circular_tree_plot_figure, self._umap_scatter_plot_figure, self._render_breadcrumb()

            node_index = point["pointIndex"]
            self.selected_cells = [node_index]
            self.selected_cell_domain_key.set(DomainSelectionConstants.USER_SELECTION).commit()
            self._initialize_circular_tree_plot()
            self._initialize_umap_scatter_plot()
            return self._circular_tree_plot_figure, self._umap_scatter_plot_figure, self._render_breadcrumb()

        @self.app.callback(
            Output("circular-tree-plot", "figure", allow_duplicate=True),
            Output("umap-scatter-plot", "figure", allow_duplicate=True),
            Output("selected-domain-label", "children", allow_duplicate=True),
            Input("umap-scatter-plot", "selectedData"),
            prevent_initial_call=True,
        )
        def _update_circular_tree_plot_based_on_umap_scatter_plot_select(selectedData):
            # A selection event is firing on initialization. Ignore it by only accepting selectedData with a range field or lasso field
            if (
                selectedData is None
                or "points" not in selectedData
                or ("range" not in selectedData and "lassoPoints" not in selectedData)
            ):
                return self._circular_tree_plot_figure, self._umap_scatter_plot_figure, self._render_breadcrumb()

            points = selectedData["points"]

            node_indexes = [point["pointIndex"] for point in points]
            self.selected_cells = node_indexes
            self.selected_cell_domain_key.set(DomainSelectionConstants.USER_SELECTION).commit()
            self._initialize_circular_tree_plot()
            self._initialize_umap_scatter_plot()
            return self._circular_tree_plot_figure, self._umap_scatter_plot_figure, self._render_breadcrumb()

        @self.app.callback(
            Output("circular-tree-plot", "figure", allow_duplicate=True),
            Output("umap-scatter-plot", "figure", allow_duplicate=True),
            Output("selected-domain-label", "children", allow_duplicate=True),
            Output("settings-pane", "children", allow_duplicate=True),
            Input("reset-selection-button", "n_clicks"),
            prevent_initial_call=True,
        )
        def _reset_selection(n_clicks):
            if n_clicks != 0:
                self._clear_selection()

                # update the figures
                self._initialize_umap_scatter_plot()
                self._initialize_circular_tree_plot()

            return (
                self._circular_tree_plot_figure,
                self._umap_scatter_plot_figure,
                self._render_breadcrumb(),
                self._render_closed_settings_pane(),
            )

        # Settings callbacks
        @self.app.callback(
            Output("circular-tree-plot", "figure", allow_duplicate=True),
            Output("umap-scatter-plot", "figure", allow_duplicate=True),
            Output("selected-domain-label", "children", allow_duplicate=True),
            Output("settings-pane", "children", allow_duplicate=True),
            Output("settings-pane", "is_open", allow_duplicate=True),
            Input("update-button", "n_clicks"),
            prevent_initial_call=True,
        )
        def _save_settings(n_clicks):
            if n_clicks > 0:
                # If a domain selection was changed and set to None, clear all selections
                if (
                    self.selected_cell_domain_key.is_dirty()
                    and self.selected_cell_domain_key.get(dirty_read=True) is DomainSelectionConstants.NONE
                ):
                    self._clear_selection()

                self.selected_cell_domain_key.commit()
                self.score_threshold.commit()
                self.min_cell_fraction.commit()

                # update the figures
                self._initialize_umap_scatter_plot()
                self._initialize_circular_tree_plot()

            return (
                self._circular_tree_plot_figure,
                self._umap_scatter_plot_figure,
                self._render_breadcrumb(),
                self._render_closed_settings_pane(),
                False,
            )

        @self.app.callback(
            Output("settings-pane", "children", allow_duplicate=True),
            Output("settings-pane", "is_open", allow_duplicate=True),
            Input("cancel-button", "n_clicks"),
            prevent_initial_call=True,
        )
        def _cancel_settings(n_clicks):
            self.selected_cell_domain_key.rollback()
            self.score_threshold.rollback()
            self.min_cell_fraction.rollback()

            return self._render_closed_settings_pane(), False

        @self.app.callback(
            Output("settings-pane", "is_open", allow_duplicate=True),
            Input("settings-button", "n_clicks"),
            [State("settings-pane", "is_open")],
            prevent_initial_call=True,
        )
        def _toggle_settings(n_clicks, is_open):
            if n_clicks:
                return not is_open
            return is_open

        @self.app.callback(
            Output("no-action", "children", allow_duplicate=True),
            Input("domain-dropdown", "value"),
            prevent_initial_call=True,
        )
        def _update_domain(domain):
            # set the domain
            self.selected_cell_domain_key.set(domain)

        @self.app.callback(
            Output("no-action", "children", allow_duplicate=True),
            Input("evidence-threshold", "value"),
            prevent_initial_call=True,
        )
        def _update_evidence_threshold(input_value):
            try:
                self.score_threshold.set(float(input_value))
            except ValueError:
                pass
            return input_value

        @self.app.callback(
            Output("no-action", "children", allow_duplicate=True),
            Input("cell-fraction", "value"),
            prevent_initial_call=True,
        )
        def _update_cell_fraction(input_value):
            try:
                self.min_cell_fraction.set(float(input_value))
            except ValueError:
                pass
            return input_value
