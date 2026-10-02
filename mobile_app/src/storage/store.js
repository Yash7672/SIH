import AsyncStorage from "@react-native-async-storage/async-storage";

const TOKEN_KEY = "rakshak_token";
const REFRESH_KEY = "rakshak_refresh";
const USER_KEY = "rakshak_user";
const DEVICE_KEY = "rakshak_device_id";

export async function loadSession() {
  try {
    const [token, refreshToken, user] = await Promise.all([
      AsyncStorage.getItem(TOKEN_KEY),
      AsyncStorage.getItem(REFRESH_KEY),
      AsyncStorage.getItem(USER_KEY),
    ]);
    if (!token || !user) return { token: null, refreshToken: null, user: null };
    return { token, refreshToken, user: JSON.parse(user) };
  } catch {
    return { token: null, refreshToken: null, user: null };
  }
}

export async function saveSession({ token, refreshToken, user }) {
  const pairs = [[TOKEN_KEY, token]];
  if (refreshToken) pairs.push([REFRESH_KEY, refreshToken]);
  if (user) pairs.push([USER_KEY, JSON.stringify(user)]);
  await AsyncStorage.multiSet(pairs);
}

export async function saveTokens(token, refreshToken) {
  const pairs = [[TOKEN_KEY, token]];
  if (refreshToken) pairs.push([REFRESH_KEY, refreshToken]);
  await AsyncStorage.multiSet(pairs);
}

export async function clearSession() {
  await AsyncStorage.multiRemove([TOKEN_KEY, REFRESH_KEY, USER_KEY]);
}

export async function getDeviceId() {
  return AsyncStorage.getItem(DEVICE_KEY);
}

export async function saveDeviceId(id) {
  await AsyncStorage.setItem(DEVICE_KEY, id);
}
