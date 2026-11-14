import logging
import os
import json
import numpy as np
from typing import List, Dict, Any, Optional, Tuple
from sklearn.cluster import (
    AgglomerativeClustering, DBSCAN, OPTICS,
    KMeans, SpectralClustering, AffinityPropagation,
    Birch, MeanShift, estimate_bandwidth
)
from sklearn.metrics import calinski_harabasz_score, davies_bouldin_score, silhouette_score
from sklearn.metrics.pairwise import cosine_similarity
from scipy.cluster.hierarchy import dendrogram, linkage, fcluster
from concurrent.futures import ProcessPoolExecutor, as_completed

logger = logging.getLogger(__name__)


def generate_embeddings(
    texts: List[str],
    model_name: str = 'bert-base-uncased',
    batch_size: int = 8
) -> np.ndarray:
    from sentence_transformers import SentenceTransformer

    model_mapping = {
        'bert-base-uncased': 'all-MiniLM-L6-v2',
        'roberta-base': 'all-mpnet-base-v2',
        'legal-bert': 'nlpaueb/legal-bert-base-uncased',
    }

    actual_model = model_mapping.get(model_name, 'all-MiniLM-L6-v2')

    logger.info(f"Loading embedding model: {actual_model}")
    model = SentenceTransformer(actual_model)

    logger.info(f"Generating embeddings for {len(texts)} texts")
    embeddings = model.encode(
        texts,
        batch_size=batch_size,
        show_progress_bar=True,
        convert_to_numpy=True
    )

    return embeddings


def perform_clustering(
    embeddings: np.ndarray,
    method: str = 'agglomerative',
    nb_cluster_method: str = 'silhouette',
    **kwargs
) -> Tuple[np.ndarray, int]:
    """
    Perform clustering on embeddings using the specified method.

    Args:
        embeddings: numpy array of embeddings
        method: Clustering method ('agglomerative', 'dbscan', 'optics',
                'kmeans', 'spectral', 'affinity_propagation', 'birch', 'mean_shift')
        nb_cluster_method: Method to determine optimal cluster count
                          ('silhouette', 'dendrogram', 'elbow')
        **kwargs: Additional parameters for the clustering algorithm

    Returns:
        Tuple of (cluster_labels, optimal_clusters)
    """
    n_samples = len(embeddings)

    # Handle 2-document edge case
    if n_samples == 2:
        sim = cosine_similarity(embeddings[0:1], embeddings[1:2])[0][0]
        if sim > 0.7:
            labels = np.array([0, 0])
        else:
            labels = np.array([0, 1])
        return labels, len(set(labels))

    if method == 'agglomerative':
        distance_threshold = kwargs.get('distance_threshold', None)
        n_clusters = kwargs.get('n_clusters', None)

        if distance_threshold is None and n_clusters is None:
            n_clusters = _find_optimal_clusters(
                embeddings, nb_cluster_method=nb_cluster_method,
                max_clusters=min(10, max(2, n_samples - 1))
            )

        clustering = AgglomerativeClustering(
            n_clusters=n_clusters,
            distance_threshold=distance_threshold,
            linkage='ward'
        )
        labels = clustering.fit_predict(embeddings)

    elif method == 'dbscan':
        eps = kwargs.get('eps', 0.5)
        min_samples = kwargs.get('min_samples', 2)

        clustering = DBSCAN(eps=eps, min_samples=min_samples, metric='cosine')
        labels = clustering.fit_predict(embeddings)

    elif method == 'optics':
        min_samples = kwargs.get('min_samples', 2)
        xi = kwargs.get('xi', 0.05)
        min_cluster_size = kwargs.get('min_cluster_size', 0.1)

        clustering = OPTICS(
            min_samples=min_samples,
            xi=xi,
            min_cluster_size=min_cluster_size,
            metric='cosine'
        )
        labels = clustering.fit_predict(embeddings)

    elif method == 'kmeans':
        n_clusters = kwargs.get('n_clusters')
        if n_clusters is None:
            n_clusters = _find_optimal_clusters(
                embeddings, nb_cluster_method=nb_cluster_method,
                max_clusters=min(10, max(2, n_samples - 1))
            )

        clustering = KMeans(
            n_clusters=n_clusters,
            max_iter=kwargs.get('max_iter', 300),
            random_state=42
        )
        labels = clustering.fit_predict(embeddings)

    elif method == 'spectral':
        n_clusters = kwargs.get('n_clusters')
        if n_clusters is None:
            n_clusters = _find_optimal_clusters(
                embeddings, nb_cluster_method=nb_cluster_method,
                max_clusters=min(10, max(2, n_samples - 1))
            )

        clustering = SpectralClustering(
            n_clusters=n_clusters,
            affinity='nearest_neighbors',
            random_state=42
        )
        labels = clustering.fit_predict(embeddings)

    elif method == 'affinity_propagation':
        damping = kwargs.get('damping', 0.5)
        preference = kwargs.get('preference', None)

        clustering = AffinityPropagation(
            damping=damping,
            preference=preference,
            random_state=42
        )
        labels = clustering.fit_predict(embeddings)

    elif method == 'birch':
        n_clusters = kwargs.get('n_clusters')
        threshold = kwargs.get('threshold', 0.5)
        branching_factor = kwargs.get('branching_factor', 50)

        if n_clusters is None:
            n_clusters = _find_optimal_clusters(
                embeddings, nb_cluster_method=nb_cluster_method,
                max_clusters=min(10, max(2, n_samples - 1))
            )

        clustering = Birch(
            n_clusters=n_clusters,
            threshold=threshold,
            branching_factor=branching_factor
        )
        labels = clustering.fit_predict(embeddings)

    elif method == 'mean_shift':
        bandwidth = kwargs.get('bandwidth', None)
        if bandwidth is None:
            try:
                bandwidth = estimate_bandwidth(embeddings, quantile=0.3)
            except Exception:
                bandwidth = 1.0

        clustering = MeanShift(bandwidth=bandwidth)
        labels = clustering.fit_predict(embeddings)

    else:
        raise ValueError(f"Unknown clustering method: {method}")

    optimal_clusters = len(set(labels)) - (1 if -1 in labels else 0)
    return labels, optimal_clusters


def _find_optimal_clusters(
    embeddings: np.ndarray,
    nb_cluster_method: str = 'silhouette',
    max_clusters: int = 10
) -> int:
    if nb_cluster_method == 'dendrogram':
        return _find_optimal_clusters_dendrogram(embeddings, max_clusters)
    elif nb_cluster_method == 'elbow':
        return _find_optimal_clusters_elbow(embeddings, max_clusters)
    else:
        return _find_optimal_clusters_silhouette(embeddings, max_clusters)


def _evaluate_silhouette_k(k: int, embeddings: np.ndarray) -> Tuple[int, float]:
    try:
        kmeans = KMeans(n_clusters=k, random_state=42, n_init=10)
        labels = kmeans.fit_predict(embeddings)
        score = silhouette_score(embeddings, labels)
        return k, score
    except Exception:
        return k, -1.0


def _find_optimal_clusters_silhouette(embeddings: np.ndarray, max_clusters: int = 10) -> int:
    n_samples = len(embeddings)
    if n_samples < 3:
        return 1

    max_k = min(max_clusters, n_samples - 1)
    best_k = 2
    best_score = -1.0

    # Use ProcessPoolExecutor for parallel evaluation
    with ProcessPoolExecutor(max_workers=min(4, max_k - 1)) as executor:
        futures = {
            executor.submit(_evaluate_silhouette_k, k, embeddings): k
            for k in range(2, max_k + 1)
        }
        for future in as_completed(futures):
            try:
                k, score = future.result()
                if score > best_score:
                    best_score = score
                    best_k = k
            except Exception as e:
                logger.debug(f"Silhouette evaluation failed for k={futures[future]}: {e}")

    return best_k


def _find_optimal_clusters_dendrogram(embeddings: np.ndarray, max_clusters: int = 10) -> int:
    linkage_matrix = linkage(embeddings, method='ward', metric='euclidean')

    # Get the last max_clusters merge distances
    n = len(embeddings)
    distances = linkage_matrix[:, 2]
    if len(distances) < 2:
        return 1

    # Find the largest gap in merge distances to determine cluster count
    sorted_distances = sorted(distances[-max_clusters:], reverse=True)
    if len(sorted_distances) < 2:
        return min(max_clusters, max(2, n - 1))

    gaps = [sorted_distances[i] - sorted_distances[i + 1]
            for i in range(len(sorted_distances) - 1)]
    if not gaps:
        return min(max_clusters, max(2, n - 1))

    largest_gap_idx = np.argmax(gaps)
    optimal_k = max(2, len(sorted_distances) - largest_gap_idx - 1)

    return min(optimal_k, max_clusters)


def _find_optimal_clusters_elbow(embeddings: np.ndarray, max_clusters: int = 10) -> int:
    n = len(embeddings)
    max_k = min(max_clusters, n - 1)

    inertias = []
    for k in range(1, max_k + 1):
        kmeans = KMeans(n_clusters=k, random_state=42, n_init=10)
        kmeans.fit(embeddings)
        inertias.append(kmeans.inertia_)

    if len(inertias) < 3:
        return max(2, len(inertias))

    # Find elbow point using the "elbow method" (max curvature)
    diffs = np.diff(inertias)
    diffs2 = np.diff(diffs)
    if len(diffs2) == 0:
        return 2

    best_k = np.argmax(diffs2) + 2
    return min(best_k, max_clusters)


def calculate_clustering_metrics(
    embeddings: np.ndarray,
    labels: np.ndarray
) -> Dict[str, float]:
    valid_mask = labels != -1
    if valid_mask.sum() < 2:
        return {'calinski_harabasz': 0.0, 'davies_bouldin': 0.0, 'silhouette': 0.0}

    valid_embeddings = embeddings[valid_mask]
    valid_labels = labels[valid_mask]

    n_clusters = len(set(valid_labels))
    if n_clusters < 2:
        return {'calinski_harabasz': 0.0, 'davies_bouldin': 0.0, 'silhouette': 0.0}

    return {
        'calinski_harabasz': calinski_harabasz_score(valid_embeddings, valid_labels),
        'davies_bouldin': davies_bouldin_score(valid_embeddings, valid_labels),
        'silhouette': silhouette_score(valid_embeddings, valid_labels)
    }


def generate_cluster_descriptions(
    cluster_texts: Dict[int, List[str]],
    api_key: Optional[str] = None,
    model: str = 'gpt-4o-mini',
    provider: str = 'openai'
) -> Dict[int, Dict[str, Any]]:
    if provider == 'ollama':
        return _generate_descriptions_ollama(cluster_texts, model)
    return _generate_descriptions_openai(cluster_texts, api_key, model)


def _generate_descriptions_ollama(
    cluster_texts: Dict[int, List[str]],
    model: str = 'mistral:latest'
) -> Dict[int, Dict[str, Any]]:
    import json
    descriptions = {}
    ollama_url = os.environ.get('OLLAMA_URL', 'http://aidocumines_ollama:11434')
    from openai import OpenAI

    client = OpenAI(base_url=f'{ollama_url}/v1', api_key='ollama')

    for cluster_id, texts in cluster_texts.items():
        sample_texts = texts[:5]
        combined_text = "\n\n---\n\n".join([t[:1000] for t in sample_texts])

        prompt = f"""Analyze the following documents that have been grouped together by a clustering algorithm.
Provide:
1. A short label (3-5 words) that describes what these documents have in common
2. A brief description (1-2 sentences) of the document type/category
3. 3-5 keywords that characterize this cluster

Documents:
{combined_text}

Respond in JSON format only:
{{"label": "...", "description": "...", "keywords": ["...", "..."]}}"""

        try:
            response = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=200,
                temperature=0.1,
            )
            from home_dash_analytics.agents.usage import record_response as _record_ai
            _record_ai(response, provider='openai', model=getattr(response, 'model', ''), task='classification')
            content = response.choices[0].message.content.strip()
            # Extract JSON from response (handle markdown-wrapped JSON)
            if '```json' in content:
                content = content.split('```json')[1].split('```')[0].strip()
            elif '```' in content:
                content = content.split('```')[1].split('```')[0].strip()

            result = json.loads(content)
            descriptions[cluster_id] = {
                'label': result.get('label', f'Cluster {cluster_id}'),
                'description': result.get('description', ''),
                'keywords': result.get('keywords', [])
            }
        except Exception as e:
            logger.error(f"Ollama description error for cluster {cluster_id}: {e}")
            descriptions[cluster_id] = {
                'label': f'Cluster {cluster_id}',
                'description': '',
                'keywords': []
            }

    return descriptions


def _generate_descriptions_openai(
    cluster_texts: Dict[int, List[str]],
    api_key: Optional[str] = None,
    model: str = 'gpt-4o-mini'
) -> Dict[int, Dict[str, Any]]:
    from openai import OpenAI

    api_key = api_key or os.environ.get('OPENAI_API_KEY')
    if not api_key:
        logger.warning("No OpenAI API key provided, skipping description generation")
        return {cid: {'label': f'Cluster {cid}', 'description': '', 'keywords': []}
                for cid in cluster_texts.keys()}

    client = OpenAI(api_key=api_key)
    descriptions = {}

    for cluster_id, texts in cluster_texts.items():
        sample_texts = texts[:5]
        combined_text = "\n\n---\n\n".join([t[:1000] for t in sample_texts])

        prompt = f"""Analyze the following documents that have been grouped together by a clustering algorithm.
Provide:
1. A short label (3-5 words) that describes what these documents have in common
2. A brief description (1-2 sentences) of the document type/category
3. 3-5 keywords that characterize this cluster

Documents:
{combined_text}

Respond in JSON format:
{{"label": "...", "description": "...", "keywords": ["...", "..."]}}"""

        try:
            response = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                max_tokens=200
            )
            from home_dash_analytics.agents.usage import record_response as _record_ai
            _record_ai(response, provider='openai', model=getattr(response, 'model', ''), task='classification')

            result = json.loads(response.choices[0].message.content)
            descriptions[cluster_id] = {
                'label': result.get('label', f'Cluster {cluster_id}'),
                'description': result.get('description', ''),
                'keywords': result.get('keywords', [])
            }
        except Exception as e:
            logger.error(f"OpenAI description error for cluster {cluster_id}: {e}")
            descriptions[cluster_id] = {
                'label': f'Cluster {cluster_id}',
                'description': '',
                'keywords': []
            }

    return descriptions
