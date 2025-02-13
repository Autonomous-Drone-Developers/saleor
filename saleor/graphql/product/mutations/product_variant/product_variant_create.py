from collections import defaultdict
from typing import List, Tuple

import graphene
from django.core.exceptions import ValidationError
from django.utils.text import slugify

from .....attribute import AttributeInputType
from .....attribute import models as attribute_models
from .....core.permissions import ProductPermissions
from .....core.tracing import traced_atomic_transaction
from .....product import models
from .....product.error_codes import ProductErrorCode
from .....product.search import update_product_search_vector
from .....product.tasks import update_product_discounted_price_task
from .....product.utils.variants import generate_and_set_variant_name
from ....attribute.types import AttributeValueInput
from ....attribute.utils import AttributeAssignmentMixin, AttrValuesInput
from ....channel import ChannelContext
from ....core.descriptions import ADDED_IN_31, ADDED_IN_38, PREVIEW_FEATURE
from ....core.mutations import ModelMutation
from ....core.scalars import WeightScalar
from ....core.types import NonNullList, ProductError
from ....core.utils import get_duplicated_values
from ....meta.mutations import MetadataInput
from ....plugins.dataloaders import load_plugin_manager
from ....warehouse.types import Warehouse
from ...types import ProductVariant
from ...utils import (
    clean_variant_sku,
    create_stocks,
    get_used_variants_attribute_values,
)
from ..product.product_create import StockInput

T_INPUT_MAP = List[Tuple[attribute_models.Attribute, AttrValuesInput]]


class PreorderSettingsInput(graphene.InputObjectType):
    global_threshold = graphene.Int(
        description="The global threshold for preorder variant."
    )
    end_date = graphene.DateTime(description="The end date for preorder.")


class ProductVariantInput(graphene.InputObjectType):
    attributes = NonNullList(
        AttributeValueInput,
        required=False,
        description="List of attributes specific to this variant.",
    )
    sku = graphene.String(description="Stock keeping unit.")
    name = graphene.String(description="Variant name.", required=False)
    track_inventory = graphene.Boolean(
        description=(
            "Determines if the inventory of this variant should be tracked. If false, "
            "the quantity won't change when customers buy this item."
        )
    )
    weight = WeightScalar(description="Weight of the Product Variant.", required=False)
    preorder = PreorderSettingsInput(
        description=(
            "Determines if variant is in preorder." + ADDED_IN_31 + PREVIEW_FEATURE
        )
    )
    quantity_limit_per_customer = graphene.Int(
        required=False,
        description=(
            "Determines maximum quantity of `ProductVariant`,"
            "that can be bought in a single checkout." + ADDED_IN_31 + PREVIEW_FEATURE
        ),
    )
    metadata = NonNullList(
        MetadataInput,
        description=(
            "Fields required to update the product variant metadata." + ADDED_IN_38
        ),
        required=False,
    )
    private_metadata = NonNullList(
        MetadataInput,
        description=(
            "Fields required to update the product variant private metadata."
            + ADDED_IN_38
        ),
        required=False,
    )


class ProductVariantCreateInput(ProductVariantInput):
    attributes = NonNullList(
        AttributeValueInput,
        required=True,
        description="List of attributes specific to this variant.",
    )
    product = graphene.ID(
        description="Product ID of which type is the variant.",
        name="product",
        required=True,
    )
    stocks = NonNullList(
        StockInput,
        description="Stocks of a product available for sale.",
        required=False,
    )


class ProductVariantCreate(ModelMutation):
    class Arguments:
        input = ProductVariantCreateInput(
            required=True, description="Fields required to create a product variant."
        )

    class Meta:
        description = "Creates a new variant for a product."
        model = models.ProductVariant
        object_type = ProductVariant
        permissions = (ProductPermissions.MANAGE_PRODUCTS,)
        error_type_class = ProductError
        error_type_field = "product_errors"
        errors_mapping = {"price_amount": "price"}
        support_meta_field = True
        support_private_meta_field = True

    @classmethod
    def clean_attributes(
        cls, attributes: dict, product_type: models.ProductType
    ) -> T_INPUT_MAP:
        attributes_qs = product_type.variant_attributes.all()
        attributes = AttributeAssignmentMixin.clean_input(attributes, attributes_qs)
        return attributes

    @classmethod
    def validate_duplicated_attribute_values(
        cls, attributes_data, used_attribute_values, instance=None
    ):
        attribute_values = defaultdict(list)
        for attr, attr_data in attributes_data:
            if attr.input_type == AttributeInputType.FILE:
                values = (
                    [slugify(attr_data.file_url.split("/")[-1])]
                    if attr_data.file_url
                    else []
                )
            else:
                values = attr_data.values
            attribute_values[attr_data.global_id].extend(values)
        if attribute_values in used_attribute_values:
            raise ValidationError(
                "Duplicated attribute values for product variant.",
                code=ProductErrorCode.DUPLICATED_INPUT_ITEM.value,
                params={"attributes": attribute_values.keys()},
            )
        else:
            used_attribute_values.append(attribute_values)

    @classmethod
    def clean_input(
        cls, info, instance: models.ProductVariant, data: dict, input_cls=None
    ):
        cleaned_input = super().clean_input(info, instance, data)

        weight = cleaned_input.get("weight")
        if weight and weight.value < 0:
            raise ValidationError(
                {
                    "weight": ValidationError(
                        "Product variant can't have negative weight.",
                        code=ProductErrorCode.INVALID.value,
                    )
                }
            )

        quantity_limit_per_customer = cleaned_input.get("quantity_limit_per_customer")
        if quantity_limit_per_customer is not None and quantity_limit_per_customer < 1:
            raise ValidationError(
                {
                    "quantity_limit_per_customer": ValidationError(
                        (
                            "Product variant can't have "
                            "quantity_limit_per_customer lower than 1."
                        ),
                        code=ProductErrorCode.INVALID.value,
                    )
                }
            )

        stocks = cleaned_input.get("stocks")
        if stocks:
            cls.check_for_duplicates_in_stocks(stocks)

        if instance.pk:
            # If the variant is getting updated,
            # simply retrieve the associated product type
            product_type = instance.product.product_type
            used_attribute_values = get_used_variants_attribute_values(instance.product)
        else:
            # If the variant is getting created, no product type is associated yet,
            # retrieve it from the required "product" input field
            product_type = cleaned_input["product"].product_type
            used_attribute_values = get_used_variants_attribute_values(
                cleaned_input["product"]
            )

        variant_attributes_ids = {
            graphene.Node.to_global_id("Attribute", attr_id)
            for attr_id in list(
                product_type.variant_attributes.all().values_list("pk", flat=True)
            )
        }
        attributes = cleaned_input.get("attributes")
        attributes_ids = {attr["id"] for attr in attributes or []}
        invalid_attributes = attributes_ids - variant_attributes_ids
        if len(invalid_attributes) > 0:
            raise ValidationError(
                "Given attributes are not a variant attributes.",
                code=ProductErrorCode.ATTRIBUTE_CANNOT_BE_ASSIGNED.value,
                params={"attributes": invalid_attributes},
            )

        # Run the validation only if product type is configurable
        if product_type.has_variants:
            # Attributes are provided as list of `AttributeValueInput` objects.
            # We need to transform them into the format they're stored in the
            # `Product` model, which is HStore field that maps attribute's PK to
            # the value's PK.
            try:
                if attributes:
                    cleaned_attributes = cls.clean_attributes(attributes, product_type)
                    cls.validate_duplicated_attribute_values(
                        cleaned_attributes, used_attribute_values, instance
                    )
                    cleaned_input["attributes"] = cleaned_attributes
                # elif not instance.pk and not attributes:
                elif not instance.pk and (
                    not attributes
                    and product_type.variant_attributes.filter(value_required=True)
                ):
                    # if attributes were not provided on creation
                    raise ValidationError(
                        "All required attributes must take a value.",
                        ProductErrorCode.REQUIRED.value,
                    )
            except ValidationError as exc:
                raise ValidationError({"attributes": exc})
        else:
            if attributes:
                raise ValidationError(
                    "Cannot assign attributes for product type without variants",
                    ProductErrorCode.INVALID.value,
                )

        if "sku" in cleaned_input:
            cleaned_input["sku"] = clean_variant_sku(cleaned_input.get("sku"))

        preorder_settings = cleaned_input.get("preorder")
        if preorder_settings:
            cleaned_input["is_preorder"] = True
            cleaned_input["preorder_global_threshold"] = preorder_settings.get(
                "global_threshold"
            )
            cleaned_input["preorder_end_date"] = preorder_settings.get("end_date")

        return cleaned_input

    @classmethod
    def check_for_duplicates_in_stocks(cls, stocks_data):
        warehouse_ids = [stock["warehouse"] for stock in stocks_data]
        duplicates = get_duplicated_values(warehouse_ids)
        if duplicates:
            error_msg = "Duplicated warehouse ID: {}".format(", ".join(duplicates))
            raise ValidationError(
                {
                    "stocks": ValidationError(
                        error_msg, code=ProductErrorCode.UNIQUE.value
                    )
                }
            )

    @classmethod
    def get_instance(cls, info, **data):
        """Prefetch related fields that are needed to process the mutation.

        If we are updating an instance and want to update its attributes,
        # prefetch them.
        """

        object_id = data.get("id")
        object_sku = data.get("sku")
        attributes = data.get("attributes")

        if attributes:
            # Prefetches needed by AttributeAssignmentMixin and
            # associate_attribute_values_to_instance
            qs = cls.Meta.model.objects.prefetch_related(
                "product__product_type__variant_attributes__values",
                "product__product_type__attributevariant",
            )
        else:
            # Use the default queryset.
            qs = models.ProductVariant.objects.all()

        if object_id:
            return cls.get_node_or_error(
                info, object_id, only_type="ProductVariant", qs=qs
            )
        elif object_sku:
            instance = qs.filter(sku=object_sku).first()
            if not instance:
                raise ValidationError(
                    {
                        "sku": ValidationError(
                            f"Couldn't resolve to a node: {object_sku}",
                            code="not_found",
                        )
                    }
                )
            return instance
        else:
            return cls._meta.model()

    @classmethod
    def save(cls, info, instance, cleaned_input):
        new_variant = instance.pk is None
        with traced_atomic_transaction():
            instance.save()
            if not instance.product.default_variant:
                instance.product.default_variant = instance
                instance.product.save(update_fields=["default_variant", "updated_at"])
            # Recalculate the "discounted price" for the parent product
            update_product_discounted_price_task.delay(instance.product_id)
            stocks = cleaned_input.get("stocks")
            if stocks:
                cls.create_variant_stocks(instance, stocks)

            attributes = cleaned_input.get("attributes")
            if attributes:
                AttributeAssignmentMixin.save(instance, attributes)

            if not instance.name:
                generate_and_set_variant_name(instance, cleaned_input.get("sku"))

            manager = load_plugin_manager(info.context)
            update_product_search_vector(instance.product)
            event_to_call = (
                manager.product_variant_created
                if new_variant
                else manager.product_variant_updated
            )
            cls.call_event(event_to_call, instance)

    @classmethod
    def create_variant_stocks(cls, variant, stocks):
        warehouse_ids = [stock["warehouse"] for stock in stocks]
        warehouses = cls.get_nodes_or_error(
            warehouse_ids, "warehouse", only_type=Warehouse
        )
        create_stocks(variant, stocks, warehouses)

    @classmethod
    def success_response(cls, instance):
        instance = ChannelContext(node=instance, channel_slug=None)
        return super().success_response(instance)
