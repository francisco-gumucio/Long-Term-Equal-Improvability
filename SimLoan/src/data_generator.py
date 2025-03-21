import torch
import torch.nn as nn
from torch.optim import Adam

import copy
import numpy as np
from pathlib import Path
from sklearn.datasets import make_classification
from sklearn.cluster import KMeans

from utils import count_time


def to_tensor(s, x, y=None):
    if torch.is_tensor(x):
        sx = torch.cat([s, x], dim=1)
    else:
        sx = np.concatenate([s, x], axis=1)
        sx = torch.FloatTensor(sx)
    if isinstance(y, np.ndarray):
        y = torch.FloatTensor(y)
        return sx, y
    return sx


class TrueModel(nn.Module):

    def __init__(self, hiddens, path, seed=0):
        super().__init__()
        self.path = path
        layers = []
        for in_dim, out_dim in zip(hiddens[:-1], hiddens[1:]):
            layers.append(nn.Linear(in_dim, out_dim))
            layers.append(nn.ReLU(inplace=True))
        layers.pop()
        layers.append(nn.Sigmoid())
        self.model = nn.Sequential(*layers)

        self.loss_fn = nn.BCELoss()
        self.optim = Adam(self.parameters())

    def forward(self, sx):
        return self.model(sx)

    def predict(self, s, x):
        sx = to_tensor(s, x)
        pred = self(sx)
        pred_y = pred.detach().round().cpu().numpy()
        return pred_y

    def fit(self, s, x, y, patience=10):
        sx, y = to_tensor(s, x, y)

        epoch, counter = 0, 0
        best_loss = float('inf')
        while True:
            pred = self(sx)
            loss = self.loss_fn(pred, y)

            self.optim.zero_grad()
            loss.backward()
            self.optim.step()
            
            epoch += 1
            if loss.item() <= best_loss:
                torch.save(self.state_dict(), self.path)
                best_loss = loss.item()
                counter = 0
            else:
                counter += 1
                if counter == patience:
                    break
        print(f"TrueModel Fit Done in {epoch} epochs!")

    def sample(self, s, x, scale=0.8):
        sx = to_tensor(s, x)
        prob = self(sx)
        y = torch.bernoulli(prob * scale)
        return y.detach().cpu().numpy()


def generate_init_dataset(no, dim):
    # 生成X, y的数据
    X, y = make_classification(
        n_samples=no, n_features=dim, n_informative=4, n_redundant=1,
        n_clusters_per_class=2, n_classes=2, flip_y=0.01, class_sep=1.0,
        random_state=1
    )

    # 生成s的数据
    kmeans = KMeans(n_clusters=4, random_state=0).fit(X)
    s = [[1] if cluster in [0, 2] else [0] for cluster in kmeans.labels_]

    s = np.array(s)
    y = y.reshape(-1, 1)
    return s, X, y


def get_components_of_change(model, s, x):
    sx = to_tensor(s, x)
    sx.requires_grad = True
    
    # 计算x对应的梯度
    prob = model(sx)
    loss = nn.BCELoss()(prob, torch.ones_like(prob))
    loss.backward()
    x_grad = (sx.grad[:, 1:]).detach().cpu().numpy()
    # 通过处理得到合适的大小
    def process(grad):
        if max(abs(grad)) != 0.0:
            while max(abs(grad)) < 1.:
                grad *= 10
        return grad
    x_grad = np.array(list(map(process, x_grad)))
    
    # 对于没有贷款的实例：sign=1表示实例向更利于贷款方向改变，sign=-1表示不变
    # 对于贷款的实例：sign=1表示实例按时还款向更利于贷款方向改变，
    # sign=-1表示没有按时还款，向更不利于贷款方向改变
    sign = 2 * torch.bernoulli(prob) - 1
    return x_grad, sign.detach().cpu().numpy()


def generate_next_dataset(s0, x0, y0, model, seq_len, epsilon):
    max_grad = 2
    max_val = 20

    x, y = [x0], [y0]
    for i in range(seq_len - 1):
        nx = copy.deepcopy(x[-1])
        ny = copy.deepcopy(y[-1]).flatten()

        x_grad, sign = get_components_of_change(model, s0, nx)
        # 发放贷款情况：每一步的改变控制在[-2, 2]范围内
        idx = np.squeeze(ny == 1) & np.squeeze(s0 == 1)
        nx[idx] -= np.clip(epsilon * sign[idx] * x_grad[idx], a_min=-max_grad, a_max=max_grad)
        idx = np.squeeze(ny == 1) & np.squeeze(s0 == 0)
        nx[idx] -= np.clip(epsilon * sign[idx] * x_grad[idx], a_min=-max_grad, a_max=max_grad)

        idx = np.squeeze(ny == 0) & np.squeeze(s0 == 1)
        nx[idx] -= np.clip(epsilon * sign[idx] * x_grad[idx], a_min=-max_grad, a_max=max_grad)
        idx = np.squeeze(ny == 0) & np.squeeze(s0 == 0)
        nx[idx] -= np.clip(2 * epsilon * sign[idx] * x_grad[idx], a_min=-max_grad, a_max=max_grad)


        # nx[ny == 1] -= np.clip(epsilon * sign[ny == 1] * x_grad[ny == 1], a_min=-max_grad, a_max=max_grad)
        # 不发放贷款的情况：每一步的改变控制在[-2, 2]范围内
        # nx[ny == 0] -= np.clip(epsilon * sign[ny == 0] * x_grad[ny == 0], a_min=-max_grad, a_max=max_grad)
        # idx = np.squeeze(sign == 0) & np.squeeze(ny == 0)
        # nx[idx] += np.clip(epsilon * x_grad[idx], a_min=-max_val, a_max=max_val)
        
        # 控制x的大小范围为：[-15, 15]
        nx = np.clip(nx, a_min=-max_val, a_max=max_val)

        pred_ny = model.sample(s0, nx)
        x.append(nx)
        y.append(pred_ny)

    return x, y


@count_time
def generate_sequential_datasets(no, dim, seq_len, hiddens, epsilon, device, path, seed=0):
    # 初始化真实模型
    model = TrueModel(hiddens, path, seed)

    # 生成初始数据集
    s0, x0, y0 = generate_init_dataset(no, dim)
    if not Path(path).exists():
        model.fit(s0, x0, y0)
    # 重新读取最好模型
    model.load_state_dict(torch.load(path, map_location=device))
    y0 = model.sample(s0, x0)

    # 生成序列数据集
    x, y = generate_next_dataset(s0, x0, y0, model, seq_len, epsilon)

    x = np.transpose(np.array(x, dtype=np.float32), axes=(1, 0, 2))
    y = np.transpose(np.array(y, dtype=np.int32), axes=(1, 0, 2))
    return s0, x, y, model


def gen_initial_data(n, noise_factor = 1, seed = 0):
    np.random.seed(seed)
    torch.manual_seed(seed)
    s0 = torch.bernoulli(torch.empty(n,1).uniform_(0,1)).numpy()
    x0 = np.random.randn(n, 1)*noise_factor + np.sin(s0)
    z0 = np.cos(x0) + np.random.randn(n, 1)*noise_factor + np.sin(s0)
    y = torch.bernoulli(torch.from_numpy(1 /(1+  np.exp(-x0-z0+1.7))))
    return torch.from_numpy(s0), to_tensor(x0, z0), y

def sequential_data(s0, x0, y0, seq_len, hiddens, l, noise_factor = 1, seed=0):
    n = s0.size()[0]
    model = TrueModel(hiddens, seed)
    sx = to_tensor(s0, x0)
    sx.requires_grad = True
    sx = sx.to(dtype=torch.float32)
    prob = model(sx)
    loss = nn.BCELoss()(prob, torch.ones_like(prob))
    loss.backward()
    x = x0
    y= y0
    prevx = x.numpy()
    prevy = y
    s0 = s0.numpy()
    nx = np.empty_like(s0)
    nz = np.empty_like(s0)
    ny = np.empty_like(s0)
    for i in range(1, seq_len):
        loss = nn.BCELoss()(prob, torch.ones_like(prob))
        delta_y = prevy*loss
        for j in range(n):
            nx[j] = np.random.randn()*noise_factor + np.sin(s0[j]) + l*(prevx[j][0] - prevy[j].item())
            nz[j] = np.cos(nx[j]) + np.random.randn()*noise_factor + np.sin(s0[j])  + l*(prevx[j][1] - prevy[j].item())
        ny = torch.bernoulli(torch.from_numpy(1 /(1+  np.exp(-nx-nz+1.7))))
        prevx = to_tensor(nx, nz)
        x = torch.cat((x, prevx),0)
        y = torch.cat((y, ny),0)
        prevx = prevx.numpy()
        prevy = ny
    # x = np.array(x, dtype=np.float32).reshape((n, seq_len, 2))
    #y = np.array(y, dtype=np.int32).reshape(n, seq_len, 1)
    return x, y
        