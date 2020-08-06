from datetime import datetime
import json
from typing import Optional

import geoalchemy2 as ga
from shapely.geometry import shape
import sqlalchemy as sa
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.dialects.postgresql import JSONB
from stac_pydantic.shared import DATETIME_RFC339

from . import schemas
from .. import settings


BaseModel = declarative_base()


class GeojsonGeometry(ga.Geometry):
    """
    Custom geoalchemy type which returns GeoJSON
    """

    from_text = "ST_GeomFromGeoJSON"

    def result_processor(self, dialect: str, coltype):
        """Override default processer to return GeoJSON"""

        def process(value: Optional[bytes]):
            if value is not None:
                geom = ga.shape.to_shape(
                    ga.elements.WKBElement(
                        value, srid=self.srid, extended=self.extended
                    )
                )
                return json.loads(json.dumps(geom.__geo_interface__))

        return process


class Collection(BaseModel):
    __tablename__ = "collections"
    __table_args__ = {"schema": "data"}

    id = sa.Column(sa.VARCHAR(1024), nullable=False, primary_key=True)
    stac_extensions = sa.Column(sa.ARRAY(sa.VARCHAR))
    stac_version = sa.Column(sa.VARCHAR(300))
    title = sa.Column(sa.VARCHAR(1024))
    description = sa.Column(sa.VARCHAR(1024), nullable=False)
    keywords = sa.Column(sa.VARCHAR(300))
    version = sa.Column(sa.VARCHAR(300))
    license = sa.Column(sa.VARCHAR(300), nullable=False)
    providers = sa.Column(JSONB)
    extent = sa.Column(JSONB)
    links = sa.Column(JSONB)
    children = sa.orm.relationship("Item", lazy="dynamic")

    @classmethod
    def get_database_model(cls, schema: schemas.Collection) -> dict:
        return schema.dict(exclude_none=True)

    @classmethod
    def from_schema(cls, schema: schemas.Collection) -> "Collection":
        return cls(**cls.get_database_model(schema))


class Item(BaseModel):
    __tablename__ = "items"
    __table_args__ = {"schema": "data"}

    id = sa.Column(sa.VARCHAR(1024), nullable=False, primary_key=True)
    stac_extensions = sa.Column(sa.ARRAY(sa.VARCHAR))
    geometry = sa.Column(GeojsonGeometry("POLYGON", srid=4326, spatial_index=True))
    bbox = sa.Column(sa.ARRAY(sa.NUMERIC), nullable=False)
    properties = sa.Column(JSONB)
    assets = sa.Column(JSONB)
    collection_id = sa.Column(
        sa.VARCHAR(1024), sa.ForeignKey(Collection.id), nullable=False
    )
    parent_collection = sa.orm.relationship("Collection", back_populates="children")
    datetime = sa.Column(sa.TIMESTAMP, nullable=False)
    links = sa.Column(JSONB)

    @classmethod
    def get_database_model(cls, schema: schemas.Item) -> dict:
        """Decompose pydantic model to data model"""
        indexed_fields = {}
        for field in settings.INDEXED_FIELDS:
            # Use getattr to accommodate extension namespaces
            field_value = getattr(schema.properties, field)
            if field == "datetime":
                field_value = datetime.strptime(field_value, DATETIME_RFC339)
            indexed_fields[field.split(":")[-1]] = field_value

        # Exclude indexed fields from the properties jsonb field
        properties = schema.properties.dict(exclude=set(settings.INDEXED_FIELDS))
        now = datetime.utcnow().strftime(DATETIME_RFC339)
        if "created" not in properties:
            properties["created"] = now
        properties["updated"] = now

        return dict(
            collection_id=schema.collection,
            geometry=ga.shape.from_shape(shape(schema.geometry), 4326),
            properties=properties,
            **indexed_fields,
            **schema.dict(
                exclude_none=True,
                exclude=set(
                    settings.FORBIDDEN_FIELDS | {"geometry", "properties", "collection"}
                ),
            )
        )

    @classmethod
    def from_schema(cls, schema: schemas.Item) -> "Item":
        return cls(**cls.get_database_model(schema))

    @classmethod
    def get_field(cls, field_name):
        """Get a model field"""
        try:
            return getattr(cls, field_name)
        except AttributeError:
            # Use a JSONB field
            return cls.properties[(field_name)].cast(
                getattr(schemas.QueryableTypes, field_name.name)
            )


class PaginationToken(BaseModel):
    __tablename__ = "tokens"
    __table_args__ = {"schema": "data"}

    id = sa.Column(sa.VARCHAR(100), nullable=False, primary_key=True)
    keyset = sa.Column(sa.VARCHAR(1000), nullable=False)
