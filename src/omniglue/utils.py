# Copyright 2024 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Shared utility functions for OmniGlue."""
import cv2
import torch
import math
import numpy as np
from typing import Optional


def lookup_descriptor_bilinear(
    keypoint: np.ndarray, descriptor_map: np.ndarray
) -> np.ndarray:
    """Looks up descriptor value for keypoint from a dense descriptor map.

    Uses bilinear interpolation to find descriptor value at non-integer
    positions.

    Args:
        keypoint: 2-dim numpy array containing (x, y) keypoint image coordinates.
        descriptor_map: (H, W, D) numpy array representing a dense descriptor map.

    Returns:
        D-dim descriptor value at the input 'keypoint' location.

    Raises:
        ValueError, if kepoint position is out of bounds.
    """
    height, width = descriptor_map.shape[:2]
    if (
        keypoint[0] < 0
        or keypoint[0] > width
        or keypoint[1] < 0
        or keypoint[1] > height
    ):
        raise ValueError(
            "Keypoint position (%f, %f) is out of descriptor map bounds (%i w x"
            " %i h)." % (keypoint[0], keypoint[1], width, height)
        )

    x_range = [math.floor(keypoint[0])]
    if not keypoint[0].is_integer() and keypoint[0] < width - 1:
        x_range.append(x_range[0] + 1)
    y_range = [math.floor(keypoint[1])]
    if not keypoint[1].is_integer() and keypoint[1] < height - 1:
        y_range.append(y_range[0] + 1)

    bilinear_descriptor = np.zeros(descriptor_map.shape[2])
    for curr_x in x_range:
        for curr_y in y_range:
            curr_descriptor = descriptor_map[curr_y, curr_x, :]
            bilinear_scalar = (1.0 - abs(keypoint[0] - curr_x)) * (
                1.0 - abs(keypoint[1] - curr_y)
            )
            bilinear_descriptor += bilinear_scalar * curr_descriptor
    return bilinear_descriptor


def soft_assignment_to_match_matrix(
    soft_assignment: torch.Tensor, match_threshold: float
) -> torch.Tensor:
    """Converts a matrix of soft assignment values to binary yes/no match matrix.

    Searches soft_assignment for row- and column-maximum values, which indicate
    mutual nearest neighbor matches between two unique sets of keypoints. Also,
    ensures that score values for matches are above the specified threshold.

    Args:
        soft_assignment: (B, N, M) tensor, contains matching likelihood value
        between features of different sets. N is number of features in image0, and
        M is number of features in image1. Higher value indicates more likely to
        match.
        match_threshold: float, thresholding value to consider a match valid.

    Returns:
        (B, N, M) tensor of binary values. A value of 1 at index (x, y) indicates
        a match between index 'x' (out of N) in image0 and index 'y' (out of M) in
        image 1.
    """

    def _range_like(x, dim):
        return torch.arange(x.shape[dim], dtype=x.dtype)

    matches = []
    for i in range(soft_assignment.shape[0]):
        scores = soft_assignment[i, :].unsqueeze(0)

        max0 = torch.max(scores, dim=2)[0]
        indices0 = torch.argmax(scores, dim=2)
        indices1 = torch.argmax(scores, dim=1)

        mutual = _range_like(indices0, 1).unsqueeze(0) == indices1.gather(
            1, indices0
        )

        kp_ind_pairs = torch.stack(
            [_range_like(indices0, 1), indices0.squeeze()], dim=1
        )
        mutual_max0 = torch.where(
            mutual, max0, torch.zeros_like(max0)
        ).squeeze()
        sparse = torch.sparse_coo_tensor(
            kp_ind_pairs.t(), mutual_max0, scores.shape[1:]
        )
        match_matrix = sparse.to_dense()
        matches.append(match_matrix)

    match_matrix = torch.stack(matches)
    match_matrix = match_matrix > match_threshold
    return match_matrix


def visualize_matches(
    image0: np.ndarray,
    image1: np.ndarray,
    kp0: np.ndarray,
    kp1: np.ndarray,
    match_matrix: np.ndarray,
    match_labels: Optional[np.ndarray] = None,
    show_keypoints: bool = False,
    highlight_unmatched: bool = False,
    title: Optional[str] = None,
    line_width: int = 1,
    circle_radius: int = 4,
    circle_thickness: int = 2,
    rng: Optional["np.random.Generator"] = None,
):
    """Generates visualization of keypoints and matches for two images.

    Stacks image0 and image1 horizontally. In case the two images have different
    heights, scales image1 (and its keypoints) to match image0's height. Note
    that keypoints must be in (x, y) format, NOT (row, col). If match_matrix
    includes unmatched dustbins, the dustbins will be removed before visualizing
    matches.

    Args:
      image0: (H, W, 3) array containing image0 contents.
      image1: (H, W, 3) array containing image1 contents.
      kp0: (N, 2) array where each row represents (x, y) coordinates of keypoints
        in image0.
      kp1: (M, 2) array, where each row represents (x, y) coordinates of keypoints
        in image1.
      match_matrix: (N, M) binary array, where values are non-zero for keypoint
        indices making up a match.
      match_labels: (N, M) binary array, where values are non-zero for keypoint
        indices making up a ground-truth match. When None, matches from
        'match_matrix' are colored randomly. Otherwise, matches from
        'match_matrix' are colored according to accuracy (compared to labels).
      show_keypoints: if True, all image0 and image1 keypoints (including
        unmatched ones) are visualized.
      highlight_unmatched: if True, highlights unmatched keypoints in blue.
      title: if not None, adds title text to top left of visualization.
      line_width: width of correspondence line, in pixels.
      circle_radius: radius of keypoint circles, if visualized.
      circle_thickness: thickness of keypoint circles, if visualized.
      rng: np random number generator to generate the line colors.

    Returns:
      Numpy array of image0 and image1 side-by-side, with lines between matches
      according to match_matrix. If show_keypoints is True, keypoints from both
      images are also visualized.
    """
    # initialize RNG
    if rng is None:
        rng = np.random.default_rng()

    # Make copy of input param that may be modified in this function.
    kp1 = np.copy(kp1)

    # Detect unmatched dustbins.
    has_unmatched_dustbins = (match_matrix.shape[0] == kp0.shape[0] + 1) and (
        match_matrix.shape[1] == kp1.shape[0] + 1
    )

    # If necessary, resize image1 so that the pair can be stacked horizontally.
    height0 = image0.shape[0]
    height1 = image1.shape[0]
    if height0 != height1:
        scale_factor = height0 / height1
        if scale_factor <= 1.0:
            interp_method = cv2.INTER_AREA
        else:
            interp_method = cv2.INTER_LINEAR
        new_dim1 = (int(image1.shape[1] * scale_factor), height0)
        image1 = cv2.resize(image1, new_dim1, interpolation=interp_method)
        kp1 *= scale_factor

    # Create side-by-side image and add lines for all matches.
    viz = cv2.hconcat([image0, image1])
    w0 = image0.shape[1]
    matches = np.argwhere(
        match_matrix[:-1, :-1] if has_unmatched_dustbins else match_matrix
    )
    for match in matches:
        mpt0 = kp0[match[0]]
        mpt1 = kp1[match[1]]
        if isinstance(mpt0, torch.Tensor):
            mpt0 = mpt0.numpy()
        if isinstance(mpt1, torch.Tensor):
            mpt1 = mpt1.numpy()
        pt0 = (int(mpt0[0]), int(mpt0[1]))
        pt1 = (int(mpt1[0] + w0), int(mpt1[1]))
        if match_labels is None:
            color = tuple(rng.integers(0, 255, size=3).tolist())
        else:
            if match_labels[match[0], match[1]]:
                color = (0, 255, 0)
            else:
                color = (255, 0, 0)
        cv2.line(viz, pt0, pt1, color, line_width)

    # Optionally, add circles to output image to represent each keypoint.
    if show_keypoints:
        for i in range(np.shape(kp0)[0]):
            kp = kp0[i].numpy() if isinstance(kp0[i], torch.Tensor) else kp0[i]
            if (
                highlight_unmatched
                and has_unmatched_dustbins
                and match_matrix[i, -1]
            ):
                cv2.circle(
                    viz,
                    tuple(kp.astype(np.int32).tolist()),
                    circle_radius,
                    (255, 0, 0),
                    circle_thickness,
                )
            else:
                cv2.circle(
                    viz,
                    tuple(kp.astype(np.int32).tolist()),
                    circle_radius,
                    (0, 0, 255),
                    circle_thickness,
                )
        for j in range(np.shape(kp1)[0]):
            kp = kp1[j].numpy() if isinstance(kp1[j], torch.Tensor) else kp1[j]
            kp[0] += w0
            if (
                highlight_unmatched
                and has_unmatched_dustbins
                and match_matrix[-1, j]
            ):
                cv2.circle(
                    viz,
                    tuple(kp.astype(np.int32).tolist()),
                    circle_radius,
                    (255, 0, 0),
                    circle_thickness,
                )
            else:
                cv2.circle(
                    viz,
                    tuple(kp.astype(np.int32).tolist()),
                    circle_radius,
                    (0, 0, 255),
                    circle_thickness,
                )
    if title is not None:
        viz = cv2.putText(
            viz,
            title,
            (5, 30),
            cv2.FONT_HERSHEY_SIMPLEX,
            1,
            (0, 0, 255),
            2,
            cv2.LINE_AA,
        )
    return viz
