import io
from PIL import Image
import pytest
from app.services.cloud_media_storage import cloud_storage


def test_cloudinary_configuration():
    """Verifies Cloudinary is initialized and configured with qzwebcf0."""
    assert cloud_storage._cloudinary_configured is True


def test_upload_property_image_to_cloudinary():
    """Tests live upload of an image to Cloudinary and verifies CDN URL format."""
    # Create sample in-memory image
    img = Image.new("RGB", (80, 80), color="green")
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    image_bytes = buf.getvalue()

    url = cloud_storage.upload_property_image(
        image_bytes=image_bytes,
        filename="green_sample.jpg",
        content_type="image/jpeg",
        property_id="test-prop-101",
        image_type="images"
    )

    assert url is not None
    assert url.startswith("https://res.cloudinary.com/qzwebcf0/image/upload/")
    assert "properties/test-prop-101/images" in url


def test_upload_floor_plan_to_cloudinary():
    """Tests upload of a floor plan to Cloudinary under floor_plans subfolder."""
    img = Image.new("RGB", (60, 60), color="white")
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    image_bytes = buf.getvalue()

    url = cloud_storage.upload_floor_plan(
        image_bytes=image_bytes,
        filename="floor_plan_a.jpg",
        property_id="test-prop-101"
    )

    assert url is not None
    assert url.startswith("https://res.cloudinary.com/qzwebcf0/image/upload/")
    assert "properties/test-prop-101/floor_plans" in url

