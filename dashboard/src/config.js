// API base URL. Empty means relative paths (Vite proxies /api, /videos and
// /thumbnails to the backend in dev); set VITE_API_URL to reach another host.
export const API_BASE_URL = import.meta.env.VITE_API_URL || '';

// Append a query parameter whether or not the URL already has a query.
export const withParam = (url, key, value) => {
    if (!url) return url;
    const [base, hash] = url.split('#');
    const sep = base.includes('?') ? '&' : '?';
    return `${base}${sep}${encodeURIComponent(key)}=${encodeURIComponent(value)}${hash !== undefined ? `#${hash}` : ''}`;
};

export const getApiUrl = (path) => {
    if (path.startsWith('http')) return path;
    const normalizedPath = path.startsWith('/') ? path : `/${path}`;
    return `${API_BASE_URL}${normalizedPath}`;
};
