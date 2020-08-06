from dataclasses import dataclass
from enum import auto
from datetime import datetime
import operator
from types import DynamicClassAttribute
from typing import Any, Callable, Dict, List, Optional, Set, Union

from geojson_pydantic.geometries import Polygon
from pydantic import root_validator, BaseModel, Field
from shapely.geometry import Polygon as ShapelyPolygon, shape
import sqlalchemy as sa
from stac_pydantic import (
    Collection as CollectionBase,
    Item as ItemBase,
)
from stac_pydantic.shared import Link
from stac_pydantic.utils import AutoValueEnum
from stac_pydantic.api import Search
from stac_pydantic.extensions import Extensions
from stac_pydantic.item import ItemProperties
from stac_pydantic.api.search import DATETIME_RFC339
from stac_pydantic.api.extensions.fields import FieldsExtension as FieldsBase

from .decompose import CollectionGetter, ItemGetter
from .. import settings

# Be careful: https://github.com/samuelcolvin/pydantic/issues/1423#issuecomment-642797287
NumType = Union[float, int]


class Operator(str, AutoValueEnum):
    """
    Define our own operators because all operators defined in stac-pydantic are not currently supported.
    """

    eq = auto()
    ne = auto()
    lt = auto()
    le = auto()
    gt = auto()
    ge = auto()
    # TODO: These are defined in the spec but aren't currently implemented by the api
    # startsWith = auto()
    # endsWith = auto()
    # contains = auto()
    # in = auto()

    @DynamicClassAttribute
    def operator(self) -> Callable[[Any, Any], bool]:
        """Return python operator"""
        return getattr(operator, self._value_)


class Queryables(str, AutoValueEnum):
    """
    Define an enum of queryable fields and their data type.  Queryable fields are explicitly defined for two reasons:
        1. So the caller knows which fields they can query by
        2. Because JSONB queries with sqlalchemy ORM require casting the type of the field at runtime
            (see ``QueryableTypes``)

    # TODO: Let the user define these in a config file
    """

    orientation = auto()
    gsd = auto()
    epsg = "proj:epsg"
    height = auto()
    width = auto()
    minzoom = "cog:minzoom"
    maxzoom = "cog:maxzoom"
    dtype = "cog:dtype"


@dataclass
class QueryableTypes:
    """
    Define an enum of the field type of each queryable field

    # TODO: Let the user define these in a config file
    # TODO: There is a much better way of defining this field <> type mapping than two enums with same keys
    """

    orientation = sa.String
    gsd = sa.Float
    epsg = sa.Integer
    height = sa.Integer
    width = sa.Integer
    minzoom = sa.Integer
    maxzoom = sa.Integer
    dtype = sa.String


class FieldsExtension(FieldsBase):
    include: Optional[Set[str]] = set()
    exclude: Optional[Set[str]] = set()

    def _get_field_dict(self, fields: Set[str]) -> Dict:
        """
        Internal method to reate a dictionary for advanced include or exclude of pydantic fields on model export

        Ref: https://pydantic-docs.helpmanual.io/usage/exporting_models/#advanced-include-and-exclude
        """
        field_dict = {}
        for field in fields:
            if "." in field:
                parent, key = field.split(".")
                if parent not in field_dict:
                    field_dict[parent] = {key}
                else:
                    field_dict[parent].add(key)
            else:
                field_dict[field] = ...
        return field_dict

    @property
    def filter_fields(self) -> Dict:
        """
        Create dictionary of fields to include/exclude on model export based on the included and excluded fields passed
        to the API

        Ref: https://pydantic-docs.helpmanual.io/usage/exporting_models/#advanced-include-and-exclude
        """
        # Include default set of fields
        include = settings.DEFAULT_INCLUDES
        # If only include is specified, add fields to default set
        if self.include and not self.exclude:
            include = include.union(self.include)
        # If both include + exclude specified, find the difference between sets but don't remove any default fields
        # If we remove default fields we will get a validation error
        elif self.include and self.exclude:
            include = include.union(self.include) - (
                self.exclude - settings.DEFAULT_INCLUDES
            )
        return {
            "include": self._get_field_dict(include),
            "exclude": self._get_field_dict(self.exclude - settings.DEFAULT_INCLUDES),
        }


class Collection(CollectionBase):
    stac_extensions: Optional[List[str]]
    links: Optional[List[Link]]

    class Config:
        orm_mode = True
        use_enum_values = True
        getter_dict = CollectionGetter

# Create a model for the extension
class NAIP_Extension(BaseModel):
    statename: str
    cell_id: int
    quadrant: str

    # Setup extension namespace in model config
    class Config:
        allow_population_by_fieldname = True
        alias_generator = lambda field_name: f"naip:{field_name}"


class NAIP_Properties(Extensions.eo, ItemProperties):
    epsg: int = Field(..., alias="proj:epsg")


class Item(ItemBase):
    geometry: Polygon
    links: Optional[List[Link]]
    properties: NAIP_Properties


    class Config:
        json_encoders = {datetime: lambda v: v.strftime(DATETIME_RFC339)}
        use_enum_values = True
        orm_mode = True
        getter_dict = ItemGetter


class STACSearch(Search):
    # Make collections optional, default to searching all collections if none are provided
    collections: Optional[List[str]] = None
    # Override default field extension to include default fields and pydantic includes/excludes factory
    field: FieldsExtension = Field(FieldsExtension(), alias="fields")
    # Override query extension with supported operators
    query: Optional[Dict[Queryables, Dict[Operator, Any]]]
    token: Optional[str] = None

    @root_validator
    def include_query_fields(cls, values: Dict) -> Dict:
        """
        Root validator to ensure query fields are included in the API response
        """
        if values["query"]:
            query_include = set(
                [
                    k.value if k in settings.INDEXED_FIELDS else f"properties.{k.value}"
                    for k in values["query"]
                ]
            )
            if not values["field"].include:
                values["field"].include = query_include
            else:
                values["field"].include.union(query_include)
        return values

    def polygon(self) -> Optional[ShapelyPolygon]:
        """
        Convenience method to create a shapely polygon for the spatial query (either `intersects` or `bbox`)
        """
        if self.intersects:
            return shape(self.intersects)
        elif self.bbox:
            return ShapelyPolygon.from_bounds(*self.bbox)
        else:
            return None
