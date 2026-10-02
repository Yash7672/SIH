# Dev-server image: `vite` reads VITE_* variables from the process environment at
# runtime, so the LAN IP baked into the bundle never goes stale when the demo
# machine changes Wi-Fi (docker compose recreates the container, no rebuild).
FROM node:22-alpine

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

COPY . .

EXPOSE 5173
CMD ["npx", "vite", "--host", "0.0.0.0", "--port", "5173", "--strictPort"]