"""Preprocessing for mobile-scanned Kannada book pages: denoise + adaptive
thresholding to handle shadows/curvature from phone scans."""

import cv2
import numpy as np


def load_grayscale(image_path: str) -> np.ndarray:
    img = cv2.imread(image_path, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError(f"Could not read image: {image_path}")
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)


def binarize(gray: np.ndarray) -> np.ndarray:
    """Adaptive threshold + denoise. Returns a binary image where text is
    black (0) on white (255) background, robust to uneven scan lighting."""
    denoised = cv2.fastNlMeansDenoising(gray, h=10)
    binary = cv2.adaptiveThreshold(
        denoised,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        blockSize=35,
        C=15,
    )
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))
    cleaned = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel)
    return cleaned


def deskew(gray: np.ndarray, binary: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Estimate skew from text-pixel bounding box and rotate both images
    to correct it. No-op if the estimated angle is negligible."""
    coords = np.column_stack(np.where(binary == 0))
    if coords.shape[0] < 50:
        return gray, binary

    angle = cv2.minAreaRect(coords)[-1]
    if angle < -45:
        angle = -(90 + angle)
    else:
        angle = -angle

    if abs(angle) < 0.3:
        return gray, binary

    h, w = gray.shape
    center = (w // 2, h // 2)
    matrix = cv2.getRotationMatrix2D(center, angle, 1.0)
    gray_rot = cv2.warpAffine(gray, matrix, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
    binary_rot = cv2.warpAffine(binary, matrix, (w, h), flags=cv2.INTER_NEAREST, borderMode=cv2.BORDER_REPLICATE)
    return gray_rot, binary_rot


def preprocess_page(image_path: str) -> tuple[np.ndarray, np.ndarray]:
    """Returns (grayscale, binary) images ready for layout analysis."""
    gray = load_grayscale(image_path)
    binary = binarize(gray)
    gray, binary = deskew(gray, binary)
    return gray, binary
