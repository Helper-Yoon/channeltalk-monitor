const express = require('express');
const http = require('http');
const socketIO = require('socket.io');
const path = require('path');
require('dotenv').config();

const ChannelHandler = require('./channelAPI');

const app = express();
const server = http.createServer(app);
const io = socketIO(server, {
  cors: {
    origin: "*",
    methods: ["GET", "POST"]
  },
  transports: ['websocket', 'polling']
});

// Middleware
app.use(express.json());
app.use(express.static('public'));

// Channel Handler 초기화
const channelHandler = new ChannelHandler(io);

// Health check
app.get('/health', (req, res) => {
  res.json({ 
    status: 'healthy',
    timestamp: new Date().toISOString(),
    redis: channelHandler.isRedisConnected(),
    uptime: process.uptime()
  });
});

// Webhook endpoint - 모든 채널톡 이벤트를 수신
app.post('/webhook', async (req, res) => {
  try {
    // Webhook 토큰 검증
    const token = req.headers['x-webhook-token'] || req.query.token;
    if (token !== process.env.WEBHOOK_TOKEN) {
      return res.status(401).json({ error: 'Unauthorized' });
    }

    // 이벤트 처리
    const event = req.body;
    console.log(`📨 Webhook received: ${event.type}`);
    
    // 비동기로 처리 (응답은 즉시)
    setImmediate(() => {
      channelHandler.handleWebhookEvent(event);
    });

    res.status(200).json({ received: true });
  } catch (error) {
    console.error('Webhook error:', error);
    res.status(500).json({ error: 'Internal server error' });
  }
});

// Socket.io 연결 처리
io.on('connection', (socket) => {
  console.log('👤 Client connected:', socket.id);
  
  socket.on('join:dashboard', async () => {
    socket.join('dashboard');
    console.log('📊 Client joined dashboard');
    
    // 현재 미답변 상담 목록 전송
    const consultations = await channelHandler.getUnansweredConsultations();
    socket.emit('dashboard:init', consultations);
  });
  
  socket.on('disconnect', () => {
    console.log('👤 Client disconnected:', socket.id);
  });
});

// 서버 시작
const PORT = process.env.PORT || 3000;
server.listen(PORT, async () => {
  console.log(`🚀 Server running on port ${PORT}`);
  console.log(`📡 Webhook endpoint: https://channeltalk-monitor.onrender.com/webhook`);
  
  // 초기화
  await channelHandler.initialize();
});

// Graceful shutdown
process.on('SIGTERM', async () => {
  console.log('SIGTERM received, shutting down gracefully');
  await channelHandler.cleanup();
  server.close(() => {
    process.exit(0);
  });
});
