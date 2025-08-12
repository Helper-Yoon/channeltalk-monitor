// src/channelAPI.js - 완전 최적화 버전
import fetch from 'node-fetch';

export class ChannelTalkService {
  constructor(redisClient, io) {
    this.redis = redisClient;
    this.io = io;
    this.apiKey = process.env.CHANNEL_API_KEY || '688a26176fcb19aebf8b';
    this.apiSecret = process.env.CHANNEL_API_SECRET || 'a0db6c38b95c8ec4d9bb46e7c653b3e2';
    this.baseURL = 'https://api.channel.io/open/v5';
    this.managers = {}; // 매니저 정보 캐시
    this.processedChats = new Set(); // 이미 처리한 채팅 ID 저장
    this.apiCallCount = 0; // API 호출 횟수 추적
    this.lastSyncTime = 0; // 마지막 동기화 시간
    this.lastManagerLoad = 0; // 매니저 마지막 로드 시간
    this.lastFullScan = 0; // 마지막 전체 스캔 시간
    this.isProcessing = false; // 현재 처리 중인지 확인
    
    // 팀별 담당자 매핑
    this.teamMembers = {
      'SNS 1팀': ['이종민', '정주연', '이혜영', '김국현', '정다혜', '조시현', '김시윤'],
      'SNS 2팀': ['윤도우리', '신혜서', '김상아', '박은진', '오민환', '서정국'],
      'SNS 3팀': ['김진후', '김시진', '권재현', '김지원', '최호익', '김진협', '박해영'],
      'SNS 4팀': ['이민주', '전지윤', '전미란', '김채영', '김영진', '강헌준'],
      '의정부 SNS팀': ['차정환', '최수능', '구본영', '서민국', '오민경', '김범주', '동수진', '성일훈', '손진우'],
      '기타': ['채주은', '강형욱']
    };
  }

  // API 호출에 딜레이 추가 (Rate Limit 방지)
  async delay(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
  }

  async makeRequest(endpoint, options = {}) {
    // Rate limit 방지: API 호출 간 500ms 딜레이로 증가
    await this.delay(500);
    
    this.apiCallCount++;
    
    const url = `${this.baseURL}${endpoint}`;
    const headers = {
      'x-access-key': this.apiKey,
      'x-access-secret': this.apiSecret,
      'accept': 'application/json',
      ...options.headers
    };

    try {
      const response = await fetch(url, { 
        ...options, 
        headers,
        timeout: 10000 
      });
      
      if (response.status === 429) {
        // Rate limit 도달시 30초 대기 후 재시도
        console.warn('Rate limit reached, waiting 30 seconds...');
        await this.delay(30000);
        return this.makeRequest(endpoint, options);
      }
      
      if (!response.ok) {
        const errorText = await response.text();
        console.error(`API Error: ${response.status} - ${errorText}`);
        throw new Error(`HTTP ${response.status}`);
      }
      
      return await response.json();
    } catch (error) {
      console.error('Channel API request failed:', error);
      throw error;
    }
  }

  async loadManagers() {
    try {
      this.managers = {};
      let offset = 0;
      let totalManagers = 0;
      let hasMore = true;
      
      console.log('Loading ALL managers from Channel Talk...');
      
      while (hasMore) {
        try {
          const endpoint = `/managers?limit=500&offset=${offset}`;
          const data = await this.makeRequest(endpoint);
          
          if (data.managers && data.managers.length > 0) {
            data.managers.forEach(manager => {
              this.managers[manager.id] = {
                id: manager.id,
                name: manager.name || manager.displayName || manager.email || 'Unknown',
                displayName: manager.displayName,
                email: manager.email
              };
              totalManagers++;
            });
            
            console.log(`Loaded ${data.managers.length} managers (total: ${totalManagers})`);
            
            if (data.managers.length < 500) {
              hasMore = false;
            } else {
              offset += 500;
            }
          } else {
            hasMore = false;
          }
        } catch (error) {
          console.error(`Error loading managers at offset ${offset}:`, error);
          hasMore = false;
        }
      }
      
      console.log(`✅ Total managers loaded: ${totalManagers}`);
      
    } catch (error) {
      console.error('Failed to load managers:', error);
    }
  }

  findTeamByName(name) {
    if (!name || name === '미배정') return '없음';
    
    const fullName = String(name).trim();
    
    for (const [team, members] of Object.entries(this.teamMembers)) {
      if (members.includes(fullName)) {
        return team;
      }
    }
    
    for (const [team, members] of Object.entries(this.teamMembers)) {
      for (const member of members) {
        if (fullName.includes(member) || member.includes(fullName)) {
          return team;
        }
      }
    }
    
    return '기타';
  }

  // 빠른 초기 동기화 (최근 200개만)
  async quickSync() {
    if (this.isProcessing) {
      console.log('Already processing, skipping quick sync');
      return;
    }

    try {
      this.isProcessing = true;
      console.log('=== Quick sync starting ===');
      this.apiCallCount = 0;
      
      // 매니저 정보 로드
      if (Object.keys(this.managers).length === 0) {
        await this.loadManagers();
      }
      
      // 최근 200개만 빠르게 가져오기
      const data = await this.makeRequest('/user-chats?state=opened&limit=200&sortOrder=desc');
      const userChats = data.userChats || [];
      
      console.log(`Quick sync: Found ${userChats.length} recent chats`);
      
      // 미답변 상담만 빠르게 처리
      const unansweredChats = [];
      const batchSize = 5;
      
      for (let i = 0; i < userChats.length; i += batchSize) {
        const batch = userChats.slice(i, i + batchSize);
        
        await Promise.all(batch.map(async (chat) => {
          try {
            const messagesData = await this.makeRequest(`/user-chats/${chat.id}/messages?sortOrder=desc&limit=3`);
            const messages = messagesData.messages || [];
            
            const lastCustomerMessage = messages.find(m => m.personType === 'user');
            const lastManagerMessage = messages.find(m => m.personType === 'manager');
            
            if (lastCustomerMessage && (!lastManagerMessage || lastCustomerMessage.createdAt > lastManagerMessage.createdAt)) {
              // 채널톡에서 분류 정보 가져오기
              let category = '';
              const chatDetail = await this.makeRequest(`/user-chats/${chat.id}`);
              const fullChat = chatDetail.userChat || chat;
              
              // tags에서 스킬_ 정보 찾기
              if (fullChat.tags && fullChat.tags.length > 0) {
                const skillTag = fullChat.tags.find(tag => 
                  typeof tag === 'string' && tag.startsWith('스킬_')
                );
                if (skillTag) {
                  category = skillTag;
                }
              }
              
              // 담당자 정보
              let counselorName = '미배정';
              let teamName = '없음';
              
              if (fullChat.assigneeId && this.managers[fullChat.assigneeId]) {
                const assignee = this.managers[fullChat.assigneeId];
                counselorName = assignee.name || assignee.displayName || '미배정';
                teamName = this.findTeamByName(counselorName);
              }
              
              // 고객 정보
              let customerName = fullChat.name || fullChat.user?.name || fullChat.user?.phoneNumber || '익명';
              
              const consultationData = {
                id: String(chat.id),
                customerName: String(customerName),
                customerMessage: String(lastCustomerMessage.plainText || lastCustomerMessage.message || ''),
                category: String(category || ''),
                team: String(teamName),
                counselor: String(counselorName),
                waitTime: String(this.calculateWaitTime(lastCustomerMessage.createdAt)),
                createdAt: String(chat.createdAt),
                frontUpdatedAt: String(lastCustomerMessage.createdAt),
                chatUrl: `https://desk.channel.io/#/channels/197228/user_chats/${chat.id}`
              };
              
              unansweredChats.push(consultationData);
              
              // Redis에 저장
              const redisData = Object.entries(consultationData)
                .map(([key, value]) => [key, String(value)])
                .flat();
              
              await this.redis.hSet(`consultation:${chat.id}`, redisData);
              await this.redis.zAdd('consultations:waiting', {
                score: lastCustomerMessage.createdAt,
                value: String(chat.id)
              });
              await this.redis.expire(`consultation:${chat.id}`, 86400);
            }
          } catch (error) {
            console.error(`Error in quick sync for chat ${chat.id}:`, error.message);
          }
        }));
        
        // 배치 간 딜레이
        if (i + batchSize < userChats.length) {
          await this.delay(1000);
        }
      }
      
      console.log(`=== Quick sync complete: ${unansweredChats.length} unanswered chats (${this.apiCallCount} API calls) ===`);
      
      // 정렬
      unansweredChats.sort((a, b) => {
        const waitTimeDiff = parseInt(b.waitTime || 0) - parseInt(a.waitTime || 0);
        if (waitTimeDiff !== 0) return waitTimeDiff;
        
        const nameA = a.customerName || '익명';
        const nameB = b.customerName || '익명';
        return nameB.localeCompare(nameA, 'ko');
      });
      
      // 대시보드 업데이트
      this.io.to('dashboard').emit('dashboard:update', unansweredChats);
      
      // 첫 동기화 시간 설정
      this.lastSyncTime = Date.now();
      
    } catch (error) {
      console.error('Quick sync error:', error);
    } finally {
      this.isProcessing = false;
    }
  }

  // 전체 동기화
  async syncOpenChats() {
    if (this.isProcessing) {
      console.log('Already processing, skipping sync');
      return;
    }

    try {
      console.log('=== Starting sync ===');
      this.apiCallCount = 0;
      
      // 최소 동기화 간격 설정 (60초)
      const now = Date.now();
      if (now - this.lastSyncTime < 60000) {
        console.log('Skipping sync - too soon');
        return;
      }
      this.lastSyncTime = now;
      this.isProcessing = true;
      
      // 매니저 정보 로드 (최초 또는 1시간마다 갱신)
      if (Object.keys(this.managers).length === 0 || now - this.lastManagerLoad > 3600000) {
        this.lastManagerLoad = now;
        await this.loadManagers();
      } else {
        console.log(`Using cached managers (${Object.keys(this.managers).length} managers)`);
      }
      
      // 전체 스캔 여부 결정 (30분마다 전체 스캔)
      const isFullScan = (now - this.lastFullScan > 1800000) || this.lastFullScan === 0;
      
      let allUserChats = [];
      
      if (isFullScan) {
        // 전체 스캔 - 하지만 실제로는 최근 5000개만
        console.log('🔍 FULL SCAN - Fetching recent 5000 chats...');
        this.lastFullScan = now;
        
        let offset = 0;
        let hasMore = true;
        const limit = 500;
        const maxChats = 5000; // 5000개로 제한
        
        while (hasMore && allUserChats.length < maxChats) {
          try {
            const data = await this.makeRequest(`/user-chats?state=opened&limit=${limit}&offset=${offset}&sortOrder=desc`);
            const userChats = data.userChats || [];
            
            allUserChats.push(...userChats);
            console.log(`Fetched batch: ${userChats.length} chats (total: ${allUserChats.length})`);
            
            if (userChats.length < limit || allUserChats.length >= maxChats) {
              hasMore = false;
            } else {
              offset += limit;
            }
          } catch (error) {
            console.error(`Error fetching chats at offset ${offset}:`, error);
            hasMore = false;
          }
        }
      } else {
        // 빠른 스캔 (최근 200개만)
        console.log('⚡ QUICK SCAN - Fetching recent 200 chats only...');
        const data = await this.makeRequest('/user-chats?state=opened&limit=200&sortOrder=desc');
        allUserChats = data.userChats || [];
      }
      
      console.log(`✅ Total chats to process: ${allUserChats.length}`);
      
      // 기존 상담 ID 목록 가져오기
      const existingIds = await this.redis.zRange('consultations:waiting', 0, -1);
      const currentIds = new Set(allUserChats.map(chat => String(chat.id)));
      
      // 삭제된 상담 제거
      for (const id of existingIds) {
        if (!currentIds.has(id)) {
          await this.redis.del(`consultation:${id}`);
          await this.redis.zRem('consultations:waiting', id);
          console.log(`Removed closed chat: ${id}`);
        }
      }
      
      // 미답변 상담 찾기 (최적화)
      const unansweredChats = [];
      const batchSize = 3; // 더 작은 배치
      let processedCount = 0;
      let newChatsCount = 0;
      let skippedCount = 0;
      
      console.log('Processing chats for unanswered messages...');
      
      for (let i = 0; i < allUserChats.length; i += batchSize) {
        const batch = allUserChats.slice(i, i + batchSize);
        
        await Promise.all(batch.map(async (chat) => {
          try {
            processedCount++;
            
            // 진행 상황 로그 (200개마다)
            if (processedCount % 200 === 0) {
              console.log(`Progress: ${processedCount}/${allUserChats.length} (New: ${newChatsCount}, Skipped: ${skippedCount})`);
            }
            
            // 이미 처리한 채팅은 대기시간만 업데이트
            const existingData = await this.redis.hGetAll(`consultation:${chat.id}`);
            if (existingData && Object.keys(existingData).length > 0) {
              const waitTime = this.calculateWaitTime(parseInt(existingData.frontUpdatedAt));
              existingData.waitTime = String(waitTime);
              unansweredChats.push(existingData);
              skippedCount++;
              return;
            }
            
            // 새로운 채팅만 상세 정보 조회
            newChatsCount++;
            
            // 메시지만 먼저 확인
            const messagesData = await this.makeRequest(`/user-chats/${chat.id}/messages?sortOrder=desc&limit=5`);
            const messages = messagesData.messages || [];
            
            const lastCustomerMessage = messages.find(m => m.personType === 'user');
            const lastManagerMessage = messages.find(m => m.personType === 'manager');
            
            if (lastCustomerMessage && (!lastManagerMessage || lastCustomerMessage.createdAt > lastManagerMessage.createdAt)) {
              // 미답변인 경우만 상세 정보 조회
              const chatDetail = await this.makeRequest(`/user-chats/${chat.id}`);
              const fullChat = chatDetail.userChat || chat;
              
              // 분류 정보
              let category = '';
              if (fullChat.tags && fullChat.tags.length > 0) {
                const skillTag = fullChat.tags.find(tag => 
                  typeof tag === 'string' && tag.startsWith('스킬_')
                );
                if (skillTag) {
                  category = skillTag;
                }
              }
              
              // 담당자 정보
              let counselorName = '미배정';
              let teamName = '없음';
              
              if (fullChat.assigneeId) {
                if (this.managers[fullChat.assigneeId]) {
                  const assignee = this.managers[fullChat.assigneeId];
                  counselorName = assignee.name || assignee.displayName || '미배정';
                  teamName = this.findTeamByName(counselorName);
                } else {
                  // 캐시에 없으면 개별 조회
                  try {
                    const managerData = await this.makeRequest(`/managers/${fullChat.assigneeId}`);
                    if (managerData.manager) {
                      const manager = managerData.manager;
                      counselorName = manager.name || manager.displayName || '미배정';
                      teamName = this.findTeamByName(counselorName);
                      
                      // 캐시에 추가
                      this.managers[fullChat.assigneeId] = {
                        id: manager.id,
                        name: counselorName,
                        displayName: manager.displayName,
                        email: manager.email
                      };
                    }
                  } catch (err) {
                    counselorName = '확인필요';
                    teamName = '확인필요';
                  }
                }
              }
              
              // 고객 정보
              let customerName = '익명';
              if (fullChat.user) {
                customerName = fullChat.user.name || 
                             fullChat.user.phoneNumber ||
                             fullChat.user.id ||
                             '익명';
              } else {
                customerName = fullChat.name || chat.name || '익명';
              }
              
              const consultationData = {
                id: String(chat.id),
                customerName: String(customerName),
                customerMessage: String(lastCustomerMessage.plainText || lastCustomerMessage.message || ''),
                category: String(category || ''),
                team: String(teamName),
                counselor: String(counselorName),
                waitTime: String(this.calculateWaitTime(lastCustomerMessage.createdAt)),
                createdAt: String(chat.createdAt),
                frontUpdatedAt: String(lastCustomerMessage.createdAt),
                chatUrl: `https://desk.channel.io/#/channels/197228/user_chats/${chat.id}`
              };
              
              unansweredChats.push(consultationData);
              
              // Redis에 저장
              const redisData = Object.entries(consultationData)
                .map(([key, value]) => [key, String(value)])
                .flat();
              
              await this.redis.hSet(`consultation:${chat.id}`, redisData);
              await this.redis.zAdd('consultations:waiting', {
                score: lastCustomerMessage.createdAt,
                value: String(chat.id)
              });
              await this.redis.expire(`consultation:${chat.id}`, 86400);
            }
          } catch (error) {
            console.error(`Error processing chat ${chat.id}:`, error.message);
          }
        }));
        
        // 배치 간 딜레이 (Rate limit 방지)
        if (i + batchSize < allUserChats.length) {
          await this.delay(2000); // 2초로 증가
        }
      }
      
      const scanType = isFullScan ? 'FULL SCAN' : 'QUICK SCAN';
      console.log(`=== ${scanType} complete: ${unansweredChats.length} unanswered (${newChatsCount} new, ${skippedCount} cached) from ${allUserChats.length} total (${this.apiCallCount} API calls) ===`);
      
      // 정렬: 대기시간 내림차순
      unansweredChats.sort((a, b) => {
        const waitTimeDiff = parseInt(b.waitTime || 0) - parseInt(a.waitTime || 0);
        if (waitTimeDiff !== 0) return waitTimeDiff;
        
        const nameA = a.customerName || '익명';
        const nameB = b.customerName || '익명';
        return nameB.localeCompare(nameA, 'ko');
      });
      
      // 대시보드 업데이트
      this.io.to('dashboard').emit('dashboard:update', unansweredChats);
      
    } catch (error) {
      console.error('Sync error:', error);
    } finally {
      this.isProcessing = false;
    }
  }

  calculateWaitTime(timestamp) {
    const now = Date.now();
    const created = parseInt(timestamp) || 0;
    const waitTimeMs = now - created;
    return Math.floor(waitTimeMs / 1000 / 60);
  }

  async getUnansweredConsultations() {
    try {
      const consultationIds = await this.redis.zRange('consultations:waiting', 0, -1, { REV: true });
      const consultations = [];
      
      for (const id of consultationIds) {
        const data = await this.redis.hGetAll(`consultation:${id}`);
        if (data && Object.keys(data).length > 0) {
          data.waitTime = String(this.calculateWaitTime(parseInt(data.frontUpdatedAt)));
          consultations.push(data);
        }
      }
      
      // 정렬: 대기시간 내림차순
      consultations.sort((a, b) => {
        const waitTimeDiff = parseInt(b.waitTime || 0) - parseInt(a.waitTime || 0);
        if (waitTimeDiff !== 0) return waitTimeDiff;
        
        const nameA = a.customerName || '익명';
        const nameB = b.customerName || '익명';
        return nameB.localeCompare(nameA, 'ko');
      });
      
      return consultations;
    } catch (error) {
      console.error('Error getting consultations:', error);
      return [];
    }
  }
}
