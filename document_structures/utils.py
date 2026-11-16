# document_structures/utils.py

import os
# unstructured (and the PyTorch stack behind it) loads on first use (core/lazy_imports.py).
from core.lazy_imports import lazy_callable
partition_pdf = lazy_callable("unstructured.partition.pdf", "partition_pdf")
partition_text = lazy_callable("unstructured.partition.text", "partition_text")
partition = lazy_callable("unstructured.partition.auto", "partition")

from document_structures import models
from core.models import File, Run, User

from django.db import transaction

import numpy as np

import logging

from collections import defaultdict

logger = logging.getLogger(__name__)

logger = logging.getLogger(__name__)

# Load once for efficiency if embeddings enabled
from django.utils.functional import SimpleLazyObject
from document_search.utils import _get_model
EMBEDDING_MODEL = SimpleLazyObject(_get_model)


def safe_metadata_to_dict(obj):
    """
    Recursively converts unstructured objects (e.g. CoordinatesMetadata)
    into JSON-safe types for storage.
    """
    if obj is None:
        return None
    if hasattr(obj, "to_dict"):
        return safe_metadata_to_dict(obj.to_dict())
    elif hasattr(obj, "dict"):
        return safe_metadata_to_dict(obj.dict())
    elif isinstance(obj, dict):
        return {k: safe_metadata_to_dict(v) for k, v in obj.items()}
    elif isinstance(obj, (list, tuple, set)):
        return [safe_metadata_to_dict(x) for x in obj]
    elif isinstance(obj, (str, int, float, bool)):
        return obj
    else:
        # Fallback to string
        return str(obj)


def embed_text(text: str) -> list[float]:
    """
    Convert text to embedding vector using sentence-transformers.
    """
    if not text.strip():
        return None
    embedding = EMBEDDING_MODEL.encode(text, normalize_embeddings=True)
    return embedding.tolist()


def run_partition(file: File, partition_strategy: str) -> list:
    """
    Run the unstructured partitioning based on the selected strategy.
    Returns a list of unstructured elements.
    """
    file_path = file.filepath
    logger.warning(f"→ Partitioning file: {file_path} | strategy={partition_strategy}")

    if partition_strategy == "partition_pdf":
        elements = partition_pdf(filename=file_path)
    elif partition_strategy == "partition_text":
        elements = partition_text(filename=file_path)
    elif partition_strategy == "partition_auto":
        elements = partition(filename=file_path)
    else:
        raise ValueError(f"Unknown partition strategy: {partition_strategy}")

    logger.warning(f"Partition produced {len(elements)} elements.")
    return elements


def compute_batch_embeddings(texts: list[str]) -> dict[str, list[float]]:
    """
    Compute embeddings for a list of texts in a batch.

    Returns:
        embedding_map = {
            text -> embedding_vector (list of floats)
        }
    """
    unique_texts = list({t for t in texts if t and t.strip()})
    if not unique_texts:
        return {}

    vectors = EMBEDDING_MODEL.encode(
        unique_texts,
        batch_size=128,
        show_progress_bar=True,
        normalize_embeddings=True,
    )
    return dict(zip(unique_texts, vectors.tolist()))


def generate_comparison_pairs(run1, run2):
    """
    Generate pairwise comparison jobs between all matching element types.
    Returns a list of dicts:
        {
            id: unique pair id
            type: element type
            text1: ...
            text2: ...
        }
    """
    elems1 = list(models.DocumentElement.objects.filter(run=run1))
    elems2 = list(models.DocumentElement.objects.filter(run=run2))

    logger.warning(f"Run1 - total elements: {len(elems1)}")
    logger.warning(f"Run2 - total elements: {len(elems2)}")

    def group_by_type(elements):
        from collections import defaultdict
        grouped = defaultdict(list)
        for el in elements:
            grouped[el.element_type].append(el)
        return grouped

    grouped1 = group_by_type(elems1)
    grouped2 = group_by_type(elems2)

    all_types = set(grouped1.keys()) | set(grouped2.keys())

    pairs = []
    pair_id_counter = 0

    for el_type in all_types:
        list1 = grouped1.get(el_type, [])
        list2 = grouped2.get(el_type, [])

        for el1 in list1:
            for el2 in list2:
                pairs.append({
                    "id": f"pair_{pair_id_counter}",
                    "type": el_type,
                    "text1": el1.text or "",
                    "text2": el2.text or "",
                })
                pair_id_counter += 1

        # Handle extra elements (unmatched)
        if not list2:
            for el1 in list1:
                pairs.append({
                    "id": f"pair_{pair_id_counter}",
                    "type": el_type,
                    "text1": el1.text or "",
                    "text2": None,
                    "note": "No matching element found in run2"
                })
                pair_id_counter += 1

        if not list1:
            for el2 in list2:
                pairs.append({
                    "id": f"pair_{pair_id_counter}",
                    "type": el_type,
                    "text1": None,
                    "text2": el2.text or "",
                    "note": "Extra element in run2"
                })
                pair_id_counter += 1

    return pairs


def aggregate_comparison_results(results):
    lexical_scores = []
    semantic_scores = []
    deviations = []

    for r in results:
        if "error" in r:
            deviations.append({
                "pair_id": r["pair_id"],
                "error": r["error"]
            })
            continue

        # Determine the category
        semantic_sim = r["semantic_similarity"]

        if semantic_sim >= 0.8:
            category = "HIGH"
        elif semantic_sim >= 0.5:
            category = "MEDIUM"
        else:
            category = "LOW"

        deviation_data = {
            "pair_id": r["pair_id"],
            "element_type": r["element_type"],
            "text1": r["text1"][:250] if r["text1"] else None,
            "text2": r["text2"][:250] if r["text2"] else None,
            "lexical_similarity": r["lexical_similarity"],
            "semantic_similarity": r["semantic_similarity"],
            "note": r.get("note"),
            "category": category,
        }

        deviations.append(deviation_data)

        lexical_scores.append(r["lexical_similarity"])
        semantic_scores.append(r["semantic_similarity"])

    # Sort deviations descending by semantic similarity
    deviations_sorted = sorted(
        deviations,
        key=lambda x: x.get("semantic_similarity", 0.0),
        reverse=True
    )

    avg_lexical = round(float(np.mean(lexical_scores)), 4) if lexical_scores else 0.0
    avg_semantic = round(float(np.mean(semantic_scores)), 4) if semantic_scores else 0.0

    return {
        "avg_lexical": avg_lexical,
        "avg_semantic": avg_semantic,
        "deviations": deviations_sorted,
    }





@transaction.atomic
def process_document_structures(
    file: File,
    run: Run,
    user: User,
    partition_strategy: str = "partition_auto",
    store_embeddings: bool = True,
    ds_run: models.DocumentStructureRun = None,  # Add ds_run as a parameter
) -> models.DocumentStructureRun:
    """
    Main entry point to:
    - run partition
    - save DocumentStructureRun
    - save DocumentElements, including any tables or hierarchy
    """

    logger.warning(f"Starting processing of file_id={file.id}, run_id={run.run_id}")

    if ds_run is None:
        # If no DocumentStructureRun is passed, create a new one
        ds_run = models.DocumentStructureRun.objects.create(
            run=run,
            file=file,
            user=user,
            partition_strategy=partition_strategy,
            status="Processing",
        )
    else:
        # If the DocumentStructureRun already exists, update its status
        ds_run.status = "Processing"
        ds_run.save()

    try:
        elements = run_partition(file, partition_strategy)
    except Exception as e:
        ds_run.status = "Failed"
        ds_run.error_message = str(e)
        ds_run.save()
        logger.error(f"Partitioning failed: {e}")
        raise

    logger.warning(f"→ About to process {len(elements)} elements.")

    element_objects = []
    table_objects = []
    cell_objects = []
    parent_map = {}
    order_counter = 0

    for el in elements:
        order_counter += 1
        el_type = el.category if hasattr(el, "category") else None
        el_text = el.text if hasattr(el, "text") else None
        el_metadata = safe_metadata_to_dict(el.metadata) if hasattr(el, "metadata") else None
        page_num = None
        coordinates = None

        # Extract page number if available
        if hasattr(el, "metadata") and hasattr(el.metadata, "page_number"):
            page_num = el.metadata.page_number

        # Extract coordinates
        if hasattr(el, "metadata") and hasattr(el.metadata, "coordinates"):
            coords = el.metadata.coordinates
            if coords is not None:
                coordinates = safe_metadata_to_dict(coords)

        # Compute embedding if desired
        embedding_vec = None
        if store_embeddings and el_text:
            embedding_vec = embed_text(el_text)

        de = models.DocumentElement(
            run=ds_run,
            element_type=el_type,
            text=el_text,
            metadata=el_metadata,
            parent=None,
            page_number=page_num,
            coordinates=coordinates,
            order=order_counter,
            embedding=embedding_vec,
        )

        element_objects.append(de)

        logger.warning(f"Element {order_counter}: Type={el_type} | Text='{(el_text or '')[:80]}'")

        parent_map[id(el)] = de

        if el_type == "Table" and hasattr(el, "metadata") and hasattr(el.metadata, "text_as_html"):
            html = el.metadata.text_as_html
            csv_text = el.metadata.text_as_csv if hasattr(el.metadata, "text_as_csv") else None
            json_data = el.metadata.table_as_json if hasattr(el.metadata, "table_as_json") else None

            dt = models.DocumentTable.objects.create(
                run=ds_run,
                page_number=page_num,
                order=order_counter,
                html=html,
                csv=csv_text,
                json=json_data,
            )
            table_objects.append(dt)

            if json_data:
                for row_idx, row in enumerate(json_data):
                    for col_idx, cell_text in enumerate(row):
                        cell = models.DocumentTableCell(
                            table=dt,
                            row_idx=row_idx,
                            col_idx=col_idx,
                            text=str(cell_text),
                        )
                        cell_objects.append(cell)

    # Bulk insert elements
    if element_objects:
        try:
            elements = models.DocumentElement.objects.bulk_create(element_objects)
            logger.warning(f"Saved {len(elements)} DocumentElement rows to DB.")
        except Exception as e:
            logger.error(f"bulk_create for DocumentElement failed: {e}")
            raise
    else:
        logger.warning("No elements to save.")

    # Parent relationships
    updates = []
    for el in elements:
        original_el = element_objects[elements.index(el)]
        if hasattr(original_el, "parent") and original_el.parent:
            parent_obj = parent_map.get(id(original_el.parent))
            if parent_obj:
                el.parent = parent_obj
                updates.append(el)

    if updates:
        try:
            models.DocumentElement.objects.bulk_update(updates, ["parent"])
            logger.warning(f"Updated parent relationships for {len(updates)} elements.")
        except Exception as e:
            logger.error(f"bulk_update for parent relationships failed: {e}")
            raise

    # Bulk save table cells
    if cell_objects:
        try:
            models.DocumentTableCell.objects.bulk_create(cell_objects)
            logger.warning(f"Saved {len(cell_objects)} DocumentTableCell rows.")
        except Exception as e:
            logger.error(f"bulk_create for table cells failed: {e}")
            raise

    ds_run.status = "Completed"
    ds_run.error_message = None
    ds_run.save()

    logger.warning(f"DocumentStructureRun {ds_run.id} marked as Completed.")

    return ds_run



def compare_document_runs(run1: models.DocumentStructureRun, run2: models.DocumentStructureRun):
    """
    Optimized version:
    - generates all pairs
    - computes embeddings in batch
    - computes semantic similarities efficiently
    - stores comparison result
    """

    logger.warning(f"Comparing runs {run1.id} vs {run2.id}")

    # Generate all pair combinations
    pairs = generate_comparison_pairs(run1, run2)
    logger.warning(f"Generated {len(pairs)} comparison pairs.")

    # Collect all unique texts to embed
    all_texts = []

    all_texts = list({
        pair["text1"] for pair in pairs if pair["text1"]
    }.union({
        pair["text2"] for pair in pairs if pair["text2"]
    }))


    #for pair in pairs:
    #    if pair["text1"]:
    #        all_texts.append(pair["text1"])
    #    if pair["text2"]:
    #        all_texts.append(pair["text2"])

    # Compute embeddings in one go
    embedding_map = compute_batch_embeddings(all_texts)
    logger.warning(f"Computed embeddings for {len(embedding_map)} unique text blocks.")

    # Compute similarities
    results = []
    for pair in pairs:
        text1 = pair["text1"] or ""
        text2 = pair["text2"] or ""

        if not text1.strip() and not text2.strip():
            results.append({
                "pair_id": pair["id"],
                "lexical_similarity": 0.0,
                "semantic_similarity": 0.0,
                "element_type": pair["type"],
                "text1": text1,
                "text2": text2,
                "note": "Both texts empty.",
            })
            continue

        # Compute similarities
        lexical_sim = lexical_similarity_score(text1, text2)
        semantic_sim = semantic_similarity_score_batch(text1, text2, embedding_map)

        results.append({
            "pair_id": pair["id"],
            "lexical_similarity": lexical_sim,
            "semantic_similarity": semantic_sim,
            "element_type": pair["type"],
            "text1": text1,
            "text2": text2,
            "note": pair.get("note"),
        })

    logger.warning(f"Completed pairwise similarity scoring for {len(results)} pairs.")

    # Aggregate results
    agg = aggregate_comparison_results(results)

    # Store comparison
    comparison = models.DocumentComparison.objects.create(
        run_1=run1,
        run_2=run2,
        lexical_similarity=agg["avg_lexical"],
        semantic_similarity=agg["avg_semantic"],
        deviation_report={"deviations": agg["deviations"]},
        status="Completed",
    )


    # Save all pair-level results
    pair_objects = []
    for result in results:
        pair_obj = models.DocumentElementPairComparison(
            comparison=comparison,
            element_type=result["element_type"],
            text1=result["text1"],
            text2=result["text2"],
            lexical_similarity=result["lexical_similarity"],
            semantic_similarity=result["semantic_similarity"],
            note=result.get("note"),
        )
        pair_objects.append(pair_obj)

    if pair_objects:
        models.DocumentElementPairComparison.objects.bulk_create(pair_objects)
        logger.warning(f"Saved {len(pair_objects)} DocumentElementPairComparison records.")
    else:
        logger.warning("No pair-level comparisons to save.")


    logger.warning(
        f"Comparison finished. "
        f"Avg Lexical={agg['avg_lexical']:.4f} | "
        f"Avg Semantic={agg['avg_semantic']:.4f}"
    )
    logger.warning(f"Total deviations stored: {len(agg['deviations'])}")

    return comparison


def lexical_similarity_score(text1: str, text2: str) -> float:
    logger.warning(
        f"→ lexical_similarity_score() called\n"
        f"    text1: {repr(text1[:300])}\n"
        f"    text2: {repr(text2[:300])}"
    )

    tokens1 = set(text1.lower().split())
    tokens2 = set(text2.lower().split())

    intersection = tokens1 & tokens2
    union = tokens1 | tokens2

    if not union:
        return 0.0

    return len(intersection) / len(union)



def semantic_similarity_score_batch(text1: str, text2: str, embedding_map: dict[str, list[float]]) -> float:
    """
    Compute cosine similarity between two texts using pre-computed embeddings.

    Returns:
        float between -1.0 and 1.0
    """
    emb1 = embedding_map.get(text1)
    emb2 = embedding_map.get(text2)

    if emb1 is None or emb2 is None:
        return 0.0

    v1 = np.array(emb1)
    v2 = np.array(emb2)

    norm1 = np.linalg.norm(v1)
    norm2 = np.linalg.norm(v2)

    if norm1 == 0 or norm2 == 0:
        return 0.0

    cosine_sim = float(np.dot(v1, v2) / (norm1 * norm2))
    return cosine_sim

