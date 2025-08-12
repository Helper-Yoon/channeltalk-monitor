const express = require('express');
const { createServer } = require('http');
const { Server } = require('socket.io');
const path = require('path');
const { initializeRedis } = require('./redisClient');
const { setupWebhook } = require('./webhookHandler');
const { ChannelTalkService } = require('./channelAPI');

const app = express();
const server = createServer(app);
const io = new Server(server, {
  cors: { origin: '*' },
  transports: ['websocket', 'polling']
});

// 환경변수
const PORT = process.env.PORT || 10000;
const HOST = '0.0.0.0';

// 미들웨어
app.use(express.raw({ type: 'application/json' }));
app.use(express.static('public'));

// 글로벌 서비스 초기화
let redisClient;
let channelService;

async function initialize() {
  // Redis 연결
  redisClient = await initializeRedis();
  
  // 채널톡 서비스 초기화
  channelService = new ChannelTalkService(redisClient, io);
  
  // 웹훅 설정
  setupWebhook(app, redisClient, io);
  
  // 초기 데이터 로드 및 30초마다 동기화
  await channelService.syncOpenChats();
  setInterval(() => channelService.syncOpenChats(), 30000);
  
  // WebSocket 연결 처리
  io.on('connection', (socket) => {
    console.log('Client connected:', socket.id);
    
    socket.on('join:dashboard', async () => {
      socket.join('dashboard');
      const currentData = await channelService.getUnansweredConsultations();
      socket.emit('dashboard:init', currentData);
    });
    
    socket.on('disconnect', () => {
      console.log('Client disconnected:', socket.id);
    });
  });
  
  // Health check 엔드포인트
  app.get('/health', async (req, res) => {
    try {
      await redisClient.ping();
      res.status(200).json({ 
        status: 'OK', 
        timestamp: new Date().toISOString(),
        redis: 'connected'
      });
    } catch (error) {
      res.status(503).json({ status: 'ERROR', error: error.message });
    }
  });
  
  server.listen(PORT, HOST, () => {
    console.log(`Server running on http://${HOST}:${PORT}`);
  });
}

// 우아한 종료 처리
process.on('SIGTERM', async () => {
  console.log('SIGTERM received, shutting down gracefully');
  server.close(async () => {
    await redisClient?.quit();
    process.exit(0);
  });
});

initialize().catch(console.error);
