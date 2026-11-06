export function authHeader() {
  return { Authorization: "Bearer " + sessionStorage.getItem("token") };
}

export function tokenExpiry(token) {
  try {
    const part = token.split(".")[1];
    const body = JSON.parse(atob(part.replace(/-/g, "+").replace(/_/g, "/")));
    return typeof body.exp === "number" ? body.exp * 1000 : 0;
  } catch {
    return 0;
  }
}

export async function authed(url, options, onExpired) {
  const response = await fetch(url, options || {});
  const headers = (options && options.headers) || {};
  if (response.status === 401 && (headers.Authorization || headers.authorization)) {
    onExpired();
    throw new Error("Your session has ended. Sign in again.");
  }
  return response;
}

export async function readError(response) {
  const body = await response.json().catch(() => ({}));
  const detail = body.detail;
  throw new Error(typeof detail === "string" ? detail : response.statusText);
}
