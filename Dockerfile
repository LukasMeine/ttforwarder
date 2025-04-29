FROM node:18-alpine
WORKDIR /app
COPY . .
RUN npm install
RUN ls
CMD ["node", "bot.js"]
