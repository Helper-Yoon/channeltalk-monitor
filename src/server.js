import express from 'express';
import { createServer } from 'http';
import { Server } from 'socket.io';
import { fileURLToPath } from 'url';
import { dirname, join } from 'path';
import { initializeRedis } from './redisClient.js';
import { setupWebhook } from './webhookHandler.js';
import { ChannelTalkService } from './channelAPI.js';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

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
app.use(express.static(join(__dirname, '../public')));

// 글로벌 서비스
let redisClient;
let channelService;

async function initialize() {
  try {
    // Redis 연결
    console.log('Connecting to Redis...');
    redisClient = await initializeRedis();
    
    // 채널톡 서비스 초기화
    console.log('Initializing Channel Talk service...');
    channelService = new ChannelTalkService(redisClient, io);
    
    // 웹훅 설정
    console.log('Setting up webhooks...');
    setupWebhook(app, redisClient, io, channelService);
    
    // 초기 데이터 로드
    console.log('Loading initial data...');
    await channelService.syncOpenChats();
    
    // 30초마다 동기화
    setInterval(() => {
      channelService.syncOpenChats().catch(console.error);
    }, 30000);
    
    // WebSocket 연결 처리
    io.on('connection', (socket) => {
      console.log('Client connected:', socket.id);
      
      socket.on('join:dashboard', async () => {
        socket.join('dashboard');
        try {
          const currentData = await channelService.getUnansweredConsultations();
          socket.emit('dashboard:init', currentData);
        } catch (error) {
          console.error('Error sending initial data:', error);
        }
      });
      
      socket.on('disconnect', () => {
        console.log('Client disconnected:', socket.id);
      });
    });
    
    // Health check
    app.get('/health', async (req, res) => {
      try {
        if (redisClient) {
          await redisClient.ping();
        }
        res.status(200).json({ 
          status: 'OK', 
          timestamp: new Date().toISOString(),
          redis: redisClient ? 'connected' : 'disconnected'
        });
      } catch (error) {
        res.status(503).json({ 
          status: 'ERROR', 
          error: error.message 
        });
      }
    });
    
    // 서버 시작
    server.listen(PORT, HOST, () => {
      console.log(`✅ Server running on http://${HOST}:${PORT}`);
    });
    
  } catch (error) {
    console.error('Initialization error:', error);
    process.exit(1);
  }
}

// 우아한 종료
process.on('SIGTERM', async () => {
  console.log('SIGTERM received, shutting down...');
  server.close(async () => {
    if (redisClient) {
      await redisClient.quit();
    }
    process.exit(0);
  });
});

initialize();
