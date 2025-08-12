const axios = require('axios');
const { createClient } = require('redis');
const TeamManager = require('./teamManager');

class ChannelHandler {
  constructor(io) {
    this.io = io;
    this.apiKey = process.env.CHANNEL_API_KEY;
    this.apiSecret = process.env.CHANNEL_API_SECRET;
    this.channelId = process.env.CHANNEL_ID || '197228'; // 기본값 설정
    this.teamManager = new TeamManager();
    
    // 디버깅용 로그
    console.log('Channel ID initialized:', this.channelId);
    
    // Redis 클라이언트
    this.redis = null;
    this.connectRedis();
    
    // 캐시
    this.managers = {};
    this.lastManagerLoad = 0;
  }

  async connectRedis() {
    try {
      this.redis = createClient({
        url: process.env.REDIS_URL
      });
      
      this.redis.on('error', (err) => console.error('Redis error:', err));
      this.redis.on('connect', () => console.log('✅ Redis connected'));
      
      await this.redis.connect();
    } catch (error) {
      console.error('Failed to connect Redis:', error);
    }
  }

  isRedisConnected() {
    return this.redis && this.redis.isReady;
  }

  async initialize() {
    console.log('🔧 Initializing Channel Handler...');
    
    // 0. 잘못된 데이터 정리
    await this.cleanupInvalidData();
    
    // 1. 매니저 정보 로드 (캐싱)
    await this.loadManagers();
    
    // 2. 초기 미답변 상담 로드 (최소한의 API 호출)
    await this.loadInitialConsultations();
    
    console.log('✅ Initialization complete');
  }

  // 잘못된 데이터 정리
  async cleanupInvalidData() {
    try {
      console.log('🧹 Cleaning up invalid data...');
      const chatIds = await this.redis.zRange('consultations:waiting', 0, -1);
      let fixedCount = 0;
      
      for (const chatId of chatIds) {
        const data = await this.redis.hGetAll(`consultation:${chatId}`);
        if (data) {
          let needsUpdate = false;
          
          // chatUrl 검증 및 수정
          if (!data.chatUrl || data.chatUrl.includes('undefined')) {
            data.chatUrl = `https://desk.channel.io/#/channels/197228/user_chats/${chatId}`;
            needsUpdate = true;
            fixedCount++;
          }
          
          // ID가 없으면 추가
          if (!data.id) {
            data.id = chatId;
            needsUpdate = true;
          }
          
          if (needsUpdate) {
            await this.redis.hSet(`consultation:${chatId}`, 
              Object.entries(data).flat()
            );
          }
        }
      }
      
      if (fixedCount > 0) {
        console.log(`✅ Fixed ${fixedCount} consultations with invalid URLs`);
      }
    } catch (error) {
      console.error('Error cleaning up invalid data:', error);
    }
  }

  // API 호출 헬퍼
  async makeRequest(endpoint, options = {}) {
    try {
      const response = await axios({
        method: options.method || 'GET',
        url: `https://api.channel.io/open/v5${endpoint}`,
        headers: {
          'X-Access-Key': this.apiKey,
          'X-Access-Secret': this.apiSecret,
          'Content-Type': 'application/json'
        },
        data: options.data,
        timeout: 10000
      });
      return response.data;
    } catch (error) {
      console.error(`API Error [${endpoint}]:`, error.message);
      throw error;
    }
  }

  // 매니저 정보 로드 (1시간마다 갱신)
  async loadManagers() {
    try {
      const now = Date.now();
      if (this.managers && Object.keys(this.managers).length > 0 && 
          now - this.lastManagerLoad < 3600000) {
        return;
      }

      console.log('📥 Loading managers...');
      let offset = 0;
      let managers = {};
      
      while (true) {
        const data = await this.makeRequest(`/managers?limit=100&offset=${offset}`);
        if (!data.managers || data.managers.length === 0) break;
        
        data.managers.forEach(m => {
          managers[m.id] = {
            id: m.id,
            name: m.displayName || m.name,
            email: m.email
          };
        });
        
        if (data.managers.length < 100) break;
        offset += 100;
      }
      
      this.managers = managers;
      this.lastManagerLoad = now;
      
      // Redis에 캐싱
      await this.redis.hSet('cache:managers', 
        Object.entries(managers).map(([k, v]) => [k, JSON.stringify(v)]).flat()
      );
      await this.redis.expire('cache:managers', 3600);
      
      console.log(`✅ Loaded ${Object.keys(managers).length} managers`);
    } catch (error) {
      console.error('Failed to load managers:', error);
    }
  }

  // 초기 미답변 상담 로드 (서버 시작 시 1회)
  async loadInitialConsultations() {
    try {
      console.log('📥 Loading initial consultations...');
      
      // 최근 500개만 빠르게 스캔
      const data = await this.makeRequest('/user-chats?state=opened&limit=500&sortOrder=desc');
      const userChats = data.userChats || [];
      
      let unansweredCount = 0;
      let answeredCount = 0;
      
      // 배치 처리 (10개씩)
      for (let i = 0; i < userChats.length; i += 10) {
        const batch = userChats.slice(i, i + 10);
        
        await Promise.all(batch.map(async (chat) => {
          try {
            // 최근 메시지 5개 확인 (봇 메시지 건너뛰기 위해)
            const messagesData = await this.makeRequest(
              `/user-chats/${chat.id}/messages?limit=5&sortOrder=desc`
            );
            const messages = messagesData.messages || [];
            
            if (messages.length > 0) {
              // 봇/시스템 메시지 제외하고 마지막 실제 메시지 찾기
              const lastRealMessage = messages.find(m => 
                m.personType === 'user' || m.personType === 'manager'
              );
              
              if (lastRealMessage) {
                // 마지막 실제 메시지가 고객 메시지면 미답변
                if (lastRealMessage.personType === 'user') {
                  await this.saveConsultation(chat, lastRealMessage);
                  unansweredCount++;
                } 
                // 마지막 실제 메시지가 매니저면 답변완료
                else if (lastRealMessage.personType === 'manager') {
                  // 혹시 Redis에 남아있다면 제거
                  await this.removeConsultation(chat.id);
                  answeredCount++;
                }
              }
            }
          } catch (error) {
            console.error(`Error checking chat ${chat.id}:`, error.message);
          }
        }));
        
        // Rate limit 방지
        if (i + 10 < userChats.length) {
          await new Promise(resolve => setTimeout(resolve, 500));
        }
      }
      
      console.log(`✅ Initial scan complete: ${unansweredCount} unanswered, ${answeredCount} answered`);
      
      // 대시보드 업데이트
      await this.broadcastUpdate();
    } catch (error) {
      console.error('Failed to load initial consultations:', error);
    }
  }

  // Webhook 이벤트 처리 (메인 로직)
  async handleWebhookEvent(event) {
    try {
      console.log(`🔄 Processing ${event.type} event`);
      
      switch (event.type) {
        case 'message':
          await this.handleMessageEvent(event);
          break;
          
        case 'userChat':
          await this.handleUserChatEvent(event);
          break;
          
        case 'userChatAssignee':
          await this.handleAssigneeEvent(event);
          break;
          
        case 'userChatTags':
          await this.handleTagsEvent(event);
          break;
          
        default:
          console.log(`Unhandled event type: ${event.type}`);
      }
    } catch (error) {
      console.error('Webhook event processing error:', error);
    }
  }

  // 메시지 이벤트 처리
  async handleMessageEvent(event) {
    const { entity, refers } = event;
    const message = entity;
    const userChat = refers?.userChat;
    
    if (!userChat) return;
    
    console.log(`📝 Message event - Type: ${message.personType}, Chat: ${userChat.id}`);
    
    // 모든 메시지 이벤트에서 최신 상태 확인
    try {
      // 최신 메시지 3개 확인 (봇 메시지 건너뛰기 위해)
      const messagesData = await this.makeRequest(
        `/user-chats/${userChat.id}/messages?limit=5&sortOrder=desc`
      );
      const messages = messagesData.messages || [];
      
      if (messages.length > 0) {
        // 봇/시스템 메시지를 제외하고 마지막 실제 메시지 찾기
        const lastRealMessage = messages.find(m => 
          m.personType === 'user' || m.personType === 'manager'
        );
        
        if (lastRealMessage) {
          // 마지막 실제 메시지가 고객 메시지인 경우 - 미답변
          if (lastRealMessage.personType === 'user') {
            console.log(`💬 Unanswered - Customer is waiting in chat ${userChat.id}`);
            await this.saveConsultation(userChat, lastRealMessage);
            
            // 실시간 알림
            this.io.to('dashboard').emit('consultation:new', {
              id: String(userChat.id),
              customerName: userChat.name || '익명',
              message: lastRealMessage.plainText || lastRealMessage.message
            });
          }
          // 마지막 실제 메시지가 매니저 메시지인 경우 - 답변됨
          else if (lastRealMessage.personType === 'manager') {
            console.log(`✅ Answered - Manager replied to chat ${userChat.id}`);
            await this.removeConsultation(userChat.id);
          }
        }
      }
    } catch (error) {
      console.error(`Error checking messages for chat ${userChat.id}:`, error);
      
      // 폴백: 원래 이벤트 기반 처리
      if (message.personType === 'user') {
        await this.saveConsultation(userChat, message);
      } else if (message.personType === 'manager') {
        await this.removeConsultation(userChat.id);
      }
    }
    
    // 대시보드 업데이트
    await this.broadcastUpdate();
  }

  // 상담 상태 변경 이벤트
  async handleUserChatEvent(event) {
    const { entity, action } = event;
    const userChat = entity;
    
    if (action === 'closed' || userChat.state === 'closed') {
      console.log(`🔒 Chat ${userChat.id} closed`);
      await this.removeConsultation(userChat.id);
      await this.broadcastUpdate();
    }
  }

  // 담당자 변경 이벤트
  async handleAssigneeEvent(event) {
    const { refers } = event;
    const userChat = refers?.userChat;
    
    if (!userChat) return;
    
    // Redis에서 상담 정보 가져오기
    const exists = await this.redis.exists(`consultation:${userChat.id}`);
    if (exists) {
      const assigneeId = event.entity?.managerId || userChat.assigneeId;
      const manager = this.managers[assigneeId];
      
      if (manager) {
        await this.redis.hSet(`consultation:${userChat.id}`, {
          counselor: manager.name,
          team: this.teamManager.getTeamByName(manager.name)
        });
        
        await this.broadcastUpdate();
      }
    }
  }

  // 태그 변경 이벤트
  async handleTagsEvent(event) {
    const { entity, refers } = event;
    const userChat = refers?.userChat;
    
    if (!userChat) return;
    
    const exists = await this.redis.exists(`consultation:${userChat.id}`);
    if (exists) {
      const tags = entity || [];
      const skillTag = tags.find(tag => tag.startsWith('스킬_'));
      
      if (skillTag) {
        await this.redis.hSet(`consultation:${userChat.id}`, {
          category: skillTag
        });
        
        await this.broadcastUpdate();
      }
    }
  }

  // 상담 정보 저장
  async saveConsultation(userChat, lastMessage) {
    try {
      // 담당자 정보
      let counselorName = '미배정';
      let teamName = '없음';
      
      if (userChat.assigneeId && this.managers[userChat.assigneeId]) {
        const assignee = this.managers[userChat.assigneeId];
        counselorName = assignee.name;
        teamName = this.teamManager.getTeamByName(counselorName);
      }
      
      // 분류 정보
      let category = '';
      if (userChat.tags && userChat.tags.length > 0) {
        const skillTag = userChat.tags.find(tag => 
          typeof tag === 'string' && tag.startsWith('스킬_')
        );
        if (skillTag) category = skillTag;
      }
      
      // 고객 정보
      const customerName = userChat.name || 
                          userChat.user?.name || 
                          userChat.user?.phoneNumber || 
                          '익명';
      
      // 대기시간 계산
      const waitTime = Math.floor((Date.now() - lastMessage.createdAt) / 60000);
      
      const consultationData = {
        id: String(userChat.id),
        customerName: String(customerName),
        customerMessage: String(lastMessage.plainText || lastMessage.message || ''),
        category: String(category),
        team: String(teamName),
        counselor: String(counselorName),
        waitTime: String(waitTime),
        createdAt: String(userChat.createdAt),
        frontUpdatedAt: String(lastMessage.createdAt),
        chatUrl: `https://desk.channel.io/#/channels/197228/user_chats/${userChat.id}`
      };
      
      // Redis에 저장
      await this.redis.hSet(
        `consultation:${userChat.id}`,
        Object.entries(consultationData).flat()
      );
      
      // Sorted Set에 추가 (대기시간 기준 정렬)
      await this.redis.zAdd('consultations:waiting', {
        score: lastMessage.createdAt,
        value: String(userChat.id)
      });
      
      // TTL 설정 (24시간)
      await this.redis.expire(`consultation:${userChat.id}`, 86400);
      
      console.log(`💾 Saved consultation ${userChat.id}`);
    } catch (error) {
      console.error(`Failed to save consultation ${userChat.id}:`, error);
    }
  }

  // 상담 제거
  async removeConsultation(chatId) {
    try {
      await this.redis.del(`consultation:${chatId}`);
      await this.redis.zRem('consultations:waiting', String(chatId));
      console.log(`🗑️ Removed consultation ${chatId}`);
    } catch (error) {
      console.error(`Failed to remove consultation ${chatId}:`, error);
    }
  }

  // 미답변 상담 목록 가져오기
  async getUnansweredConsultations() {
    try {
      // Sorted Set에서 모든 대기 중인 상담 ID 가져오기
      const chatIds = await this.redis.zRange('consultations:waiting', 0, -1);
      
      const consultations = [];
      for (const chatId of chatIds) {
        const data = await this.redis.hGetAll(`consultation:${chatId}`);
        if (data && Object.keys(data).length > 0) {
          // 대기시간 재계산
          const waitTime = Math.floor((Date.now() - parseInt(data.frontUpdatedAt)) / 60000);
          data.waitTime = String(waitTime);
          
          // chatUrl 검증 및 수정 (undefined 방지)
          if (!data.chatUrl || data.chatUrl.includes('undefined')) {
            data.chatUrl = `https://desk.channel.io/#/channels/197228/user_chats/${data.id}`;
            // Redis에도 업데이트
            await this.redis.hSet(`consultation:${chatId}`, 'chatUrl', data.chatUrl);
          }
          
          consultations.push(data);
        }
      }
      
      // 대기시간 내림차순 정렬
      consultations.sort((a, b) => parseInt(b.waitTime) - parseInt(a.waitTime));
      
      return consultations;
    } catch (error) {
      console.error('Failed to get consultations:', error);
      return [];
    }
  }

  // 대시보드 업데이트 브로드캐스트
  async broadcastUpdate() {
    const consultations = await this.getUnansweredConsultations();
    this.io.to('dashboard').emit('dashboard:update', consultations);
    console.log(`📡 Broadcasted update: ${consultations.length} consultations`);
  }

  // 답변된 상담 정리 (주기적으로 실행 - 1분마다)
  async cleanupAnsweredChats() {
    try {
      const chatIds = await this.redis.zRange('consultations:waiting', 0, -1);
      let cleanedCount = 0;
      let updatedCount = 0;
      
      // 배치 처리 (5개씩)
      for (let i = 0; i < chatIds.length; i += 5) {
        const batch = chatIds.slice(i, i + 5);
        
        await Promise.all(batch.map(async (chatId) => {
          try {
            // 각 상담의 최신 메시지 5개 확인
            const messagesData = await this.makeRequest(
              `/user-chats/${chatId}/messages?limit=5&sortOrder=desc`
            );
            const messages = messagesData.messages || [];
            
            if (messages.length > 0) {
              // 봇/시스템 메시지 제외하고 마지막 실제 메시지 찾기
              const lastRealMessage = messages.find(m => 
                m.personType === 'user' || m.personType === 'manager'
              );
              
              if (lastRealMessage) {
                // 마지막 실제 메시지가 매니저면 제거
                if (lastRealMessage.personType === 'manager') {
                  await this.removeConsultation(chatId);
                  cleanedCount++;
                }
                // 고객 메시지면 대기시간 업데이트
                else if (lastRealMessage.personType === 'user') {
                  const waitTime = Math.floor((Date.now() - lastRealMessage.createdAt) / 60000);
                  await this.redis.hSet(`consultation:${chatId}`, {
                    waitTime: String(waitTime),
                    frontUpdatedAt: String(lastRealMessage.createdAt)
                  });
                  updatedCount++;
                }
              }
            }
          } catch (error) {
            // 상담이 닫혔거나 삭제된 경우
            if (error.response?.status === 404) {
              await this.removeConsultation(chatId);
              cleanedCount++;
            }
          }
        }));
        
        // Rate limit 방지 (배치 간 짧은 대기)
        if (i + 5 < chatIds.length) {
          await new Promise(resolve => setTimeout(resolve, 200));
        }
      }
      
      if (cleanedCount > 0 || updatedCount > 0) {
        console.log(`🧹 Cleanup: ${cleanedCount} answered removed, ${updatedCount} wait times updated`);
        await this.broadcastUpdate();
      }
    } catch (error) {
      console.error('Cleanup error:', error);
    }
  }

  // 대기시간만 업데이트 (30초마다 실행)
  async updateWaitTimes() {
    try {
      const chatIds = await this.redis.zRange('consultations:waiting', 0, -1);
      
      for (const chatId of chatIds) {
        const data = await this.redis.hGetAll(`consultation:${chatId}`);
        if (data && data.frontUpdatedAt) {
          const waitTime = Math.floor((Date.now() - parseInt(data.frontUpdatedAt)) / 60000);
          await this.redis.hSet(`consultation:${chatId}`, {
            waitTime: String(waitTime)
          });
        }
      }
      
      // 대시보드에 업데이트 전송
      await this.broadcastUpdate();
    } catch (error) {
      console.error('Error updating wait times:', error);
    }
  }

  // 정리
  async cleanup() {
    if (this.redis) {
      await this.redis.quit();
    }
  }
}

module.exports = ChannelHandler;
